"""Bounded SSH transport policy and its shared failure vocabulary.

A transport fault is the ssh link itself failing (a refused control-master session, a dropped
link, a timeout) rather than the remote command running and exiting non-zero. The two read
identically on the wire, ssh exits 255 with a transport phrase on stderr, so naming them in a
single low-level place keeps the connect path (`environment.connection`) and the probe path
(`schedulers.base.login_run`) from drifting apart. They used to: the probe path retried a blip
while the connect path had no handling at all, so the same flaky link a running watcher rode out
would crash a fresh one mid-handshake with no output. Both now share `transport_failure`.
"""

import os
import shlex
import signal
import subprocess
from contextlib import suppress
from math import ceil

from .base import Field, FrozenModel
from .clients.machine import BoundedSshMachine

# ssh's own exit status when the transport fails (vs. the remote command running and exiting
# non-zero), with the stderr phrases that name a transport-level fault rather than a real answer.
SSH_TRANSPORT_RC = 255
TRANSPORT_MARKERS = (
    "session open refused",
    "connection refused",
    "connection closed",
    "connection timed out",
    "operation timed out",
    "broken pipe",
    "no route to host",
    "kex_exchange",
    "control socket",
    "control master",
    "timed out",
)


# A scheduler daemon (pueue's ``pueued``) that has died refuses its own control socket, so the
# client process exits non-zero naming the refused socket. This is distinct from an ssh transport
# fault: the link to the host is fine, the daemon behind it is down, so it surfaces as ``daemon
# down`` and ``lote revive`` restarts it. ``daemon_failure`` only ever reads a scheduler client's
# own stderr, never ssh's, so sharing ``connection refused`` with the transport markers is safe.
DAEMON_DOWN_MARKERS = ("connecting to the daemon", "connection refused", ".socket")


class HostUnreachable(Exception):
    """An ssh transport failure, so a host's state is unknown right now rather than settled.

    Raised when the ssh connection itself failed (a refused control-master session, a dropped
    link, a timeout) rather than the remote command running and exiting non-zero. The wait and
    connect loops absorb a few of these with backoff, so a transient blip is never misread as a
    finished or vanished job, nor as a host that cannot be reached at all; a persistent outage
    still surfaces once the retry budget is spent."""


class SshTransport(FrozenModel):
    """One bounded OpenSSH policy while preserving user aliases and ProxyJump settings."""

    connect_timeout: float = Field(default=15.0, gt=0.0)
    server_alive_interval: float = Field(default=15.0, gt=0.0)
    server_alive_count: int = Field(default=3, ge=1)
    batch_mode: bool = True

    @property
    def deadline(self) -> float:
        """Return the worst-case liveness window for a control operation."""
        return self.connect_timeout + self.server_alive_interval * self.server_alive_count + 5.0

    @property
    def options(self) -> tuple[str, ...]:
        """Return only the liveness overrides, leaving every alias setting intact."""
        return (
            "-o",
            f"ConnectTimeout={ceil(self.connect_timeout)}",
            "-o",
            f"ServerAliveInterval={ceil(self.server_alive_interval)}",
            "-o",
            f"ServerAliveCountMax={self.server_alive_count}",
            "-o",
            f"BatchMode={'yes' if self.batch_mode else 'no'}",
        )

    @property
    def rsync_shell(self) -> str:
        """Return rsync's remote shell under this same SSH policy."""
        return shlex.join(("ssh", *self.options))

    def warm(self, host: str) -> None:
        """Validate one bounded SSH connection before Plumbum opens its persistent session."""
        self.run(("ssh", *self.options, host, "true"), host, "connect")

    def copy(self, source: str, destination: str, *, host: str) -> None:
        """Copy one file through the bounded SSH policy."""
        self.run(("scp", *self.options, source, destination), host, "copy")

    def machine(self, host: str) -> BoundedSshMachine:
        """Return a persistent SSH session with a dedicated local process group."""
        return BoundedSshMachine(
            host,
            ssh_opts=self.options,
            connect_timeout=self.deadline,
            new_session=True,
        )

    def run(self, command: tuple[str, ...], host: str, operation: str) -> str:
        """Run one SSH transfer in a killable process group and surface a typed failure."""
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            raise HostUnreachable(
                f"ssh {operation} to {host!r} could not start: {error}"
            ) from error
        try:
            stdout, stderr = process.communicate(timeout=self.deadline)
        except subprocess.TimeoutExpired as error:
            self.terminate(process)
            raise HostUnreachable(
                f"ssh {operation} to {host!r} timed out after {self.deadline:g}s"
            ) from error
        if process.returncode == 0:
            return stdout
        if "host key verification failed" in stderr.lower():
            raise ConnectionError(f"ssh to {host!r} failed host-key verification")
        detail = (
            stderr.strip().splitlines()[-1] if stderr.strip() else f"exit {process.returncode}"
        )
        if transport_failure(process.returncode, stderr):
            raise HostUnreachable(f"ssh {operation} to {host!r} failed: {detail}")
        raise RuntimeError(f"ssh {operation} to {host!r} failed: {detail}")

    @staticmethod
    def terminate(process: subprocess.Popen[str]) -> None:
        """Terminate the whole SSH process group so ProxyJump children cannot remain."""
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class DaemonDown(HostUnreachable):
    """A host's scheduler daemon is down (a dead pueue ``pueued``), so its jobs cannot resolve now.

    A subclass of :class:`HostUnreachable`, so every wait/poll/status/monitor path that already
    rides out an unreachable host treats a dead daemon the same way rather than crashing on the
    raw client error. The reason it carries, ``daemon down``, is what the durable monitor surfaces
    per host, and ``lote revive <host>`` restarts the daemon to recover."""


def transport_failure(retcode: int, stderr: str) -> bool:
    """Whether ``(retcode, stderr)`` is an ssh transport fault, not a real command answer."""
    low = stderr.lower()
    return retcode == SSH_TRANSPORT_RC and any(marker in low for marker in TRANSPORT_MARKERS)


def daemon_failure(stderr: str) -> bool:
    """Whether a scheduler client's ``stderr`` names a dead daemon (a refused control socket)."""
    low = stderr.lower()
    return any(marker in low for marker in DAEMON_DOWN_MARKERS)
