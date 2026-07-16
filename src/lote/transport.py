"""Shared vocabulary for ssh transport faults, the one definition both paths agree on.

A transport fault is the ssh link itself failing (a refused control-master session, a dropped
link, a timeout) rather than the remote command running and exiting non-zero. The two read
identically on the wire, ssh exits 255 with a transport phrase on stderr, so naming them in a
single low-level place keeps the connect path (`environment.connection`) and the probe path
(`schedulers.base.login_run`) from drifting apart. They used to: the probe path retried a blip
while the connect path had no handling at all, so the same flaky link a running watcher rode out
would crash a fresh one mid-handshake with no output. Both now share `transport_failure`.
"""

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
