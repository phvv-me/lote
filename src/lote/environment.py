import shlex

from plumbum import SshMachine
from tenacity import (
    retry as tenacity_retry,
)
from tenacity import (
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from . import NAME
from .base import FrozenModel
from .transport import HostUnreachable, SshTransport

USER_BINS = ("$HOME/.local/bin", "$HOME/.pixi/bin", "$HOME/.cargo/bin")
# A connect-time transport blip (a stale control-master, a refused session under MaxSessions) is
# the same transient fault the wait loops ride out per probe, so the connect path retries it on the
# same footing instead of crashing a command with no output. A genuine fault still surfaces once
# these attempts are spent; a host-key failure is not transient and is never retried.
CONNECT_ATTEMPTS = 4
CONNECT_BACKOFF = 2.0


class Environment(FrozenModel):
    """How to activate a command on a host -- the single source of truth.

    Collapses the formerly divergent activation paths (the cli's cargo-only
    PATH insert, pueue's ``USER_BINS``, the schedulers' login-shell
    ``remote_exec``, and ``setup.sh``'s PATH) into one wrapper. Prepending the
    user bins on every path is what lets ``chefe`` resolve on any host instead of
    falling back to the pixi binary; a login shell additionally sources
    ``/etc/profile.d`` so an HPC host's ``qsub``/``module`` toolchain is on PATH.

    root: the repo path on the host (the working directory commands run from).
    login: wrap under ``bash -lc`` (sources the HPC toolchain) rather than ``bash -c``.
    user_bins: per-user install dirs prepended to PATH (chefe, pixi, cargo live here).
    """

    root: str
    login: bool = True
    user_bins: tuple[str, ...] = USER_BINS
    ssh: SshTransport = SshTransport()

    @property
    def path(self) -> str:
        """The user bins joined as a ``PATH`` prefix."""
        return ":".join(self.user_bins)

    def wrap(self, command: str, *, chefe: bool = True, cd: bool = True) -> str:
        """Activated body: ``cd <root> && export PATH=<bins>:$PATH && [chefe run] <command>``.

        command: the bare command to run on the host.
        chefe: run it through ``chefe run`` (inside the compiled env).
        cd: change into ``root`` first (pueue sets its own working directory, off there).
        """
        steps = [f"cd {shlex.quote(self.root)}"] if cd else []
        steps.append(f"export PATH={self.path}:$PATH")
        steps.append(f"chefe run {command}" if chefe else command)
        return " && ".join(steps)

    def argv(self, command: str, *, chefe: bool = True, cd: bool = True) -> list[str]:
        """``wrap`` under a ``bash`` login (``-lc``) or plain (``-c``) shell.

        Ready for plumbum (``machine[argv[0]][argv[1:]]``) or subprocess.
        """
        flag = "-lc" if self.login else "-c"
        return ["bash", flag, self.wrap(command, chefe=chefe, cd=cd)]

    def exec_command(self, *args: str) -> str:
        """Activated body for the on-host executor: ``chefe run lote exec <args>``.

        The single builder for every scheduler's ``lote exec qsub``/``sbatch``/
        ``run``/``status``/... call (the former ``_remote.remote_exec``); the
        caller wraps it in ``bash -lc`` so the login shell adds the cluster
        toolchain (``qsub``/``module``).
        """
        command = f"{NAME} exec " + " ".join(shlex.quote(str(arg)) for arg in args)
        return self.wrap(command, chefe=True)

    def connection(self, host: str) -> SshMachine:
        """Open an ssh connection to ``host`` with the user install dirs on PATH.

        The single place bare-tool PATH is set, so ``chefe``/``pueue``/
        ``nvidia-smi`` resolve from the same ``user_bins`` activated commands use --
        never the forbidden pixi binary. Replaces the cli's ``connect``.

        First warms the host's ssh ``ControlMaster`` (from ``~/.ssh/config``) with a
        throwaway subprocess ``ssh``: if the persistent master has expired, that slow
        relogin happens on a robust one-shot channel, so plumbum's persistent session
        then rides a live master instead of dying mid-handshake (the
        ``readline of closed file`` we hit on miyabi) during the reconnect. We do not
        set ``ControlMaster``/``ControlPath`` ourselves -- the user's config owns the
        multiplexing; overriding it would open a second, unauthenticated master.

        That same warm-up doubles as a host-key check: if it fails verification (the host
        or its ProxyJump rotated its key, or the entry is missing) we raise a clear,
        actionable error here instead of letting plumbum die with an opaque traceback. A
        transport-level warm-up failure (a refused session under MaxSessions, a dropped
        link) is retried a few times before giving up, the same transient-fault footing
        the wait loops already stand on, so a connect-time blip no longer crashes a
        command outright -- the gap that left a backgrounded watcher dead with no output.
        """
        retrying = tenacity_retry(
            retry=retry_if_exception_type(HostUnreachable),
            stop=stop_after_attempt(CONNECT_ATTEMPTS),
            wait=wait_fixed(CONNECT_BACKOFF),
            reraise=True,
        )
        return retrying(self._open)(host)

    def _open(self, host: str) -> SshMachine:
        """One attempt to open the connection: warm the master, key-check, then build the session.

        Raises :class:`HostUnreachable` on a transient transport fault (so the caller retries) and
        ``ConnectionError`` on a host-key failure (which no retry can fix)."""
        self.ssh.warm(host)
        remote = self.ssh.machine(host)
        for bindir in reversed(self.user_bins):
            remote.env.path.insert(0, remote.cwd / bindir.removeprefix("$HOME/"))
        return remote
