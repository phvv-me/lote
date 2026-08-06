"""Process-wide lock so only one ``lote wait`` watcher runs at a time.

A watcher (``lote wait``, backgrounded) holds an SSH connection open for the whole life of
a remote job. Several at once exhaust the host's ``MaxSessions`` and the surplus watchers
fail to connect while looking alive, so the rule learned the hard way is one watcher only.
The first watcher claims a lockfile stamped with its pid and handle, and a second watcher
refuses to start while that pid is alive, telling the caller which handle already owns it. A
lock left by a watcher that died is reclaimed on the spot, so a crash never wedges the slot.
"""

import os
from contextlib import contextmanager
from pathlib import Path

from . import STATE_DIR
from .log import logger

LOCK = Path(STATE_DIR) / "watcher.lock"


def alive(pid: int) -> bool:
    """Whether ``pid`` is a live process (signal 0 probes without delivering anything)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but owned by someone else, so it is alive
        return True
    return True


def holder() -> tuple[int, str] | None:
    """The ``(pid, handle)`` of a live watcher holding the lock, or ``None`` if free or stale."""
    try:
        pid_text, _, handle = LOCK.read_text().partition(" ")
    except FileNotFoundError:
        return None
    pid = int(pid_text)
    if alive(pid):
        return pid, handle.strip()
    LOCK.unlink(missing_ok=True)  # the previous watcher died, so reclaim the slot
    return None


@contextmanager
def single_watcher(handle: str):
    """Hold the one watcher slot for ``handle``; refuse to start if another watcher is live.

    Raises ``SystemExit`` naming the handle that already owns the slot, so a second
    ``lote wait`` fails loudly instead of opening a doomed extra SSH session.
    """
    owner = holder()
    if owner is not None:
        pid, owned = owner
        raise SystemExit(
            f"a watcher is already running for {owned!r} (pid {pid}); only one watcher is "
            f"allowed at a time, so wait for it or `kill {pid}` before watching {handle!r}"
        )
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, f"{os.getpid()} {handle}".encode())
    os.close(fd)
    logger.debug("watcher lock held for {} (pid {})", handle, os.getpid())
    try:
        yield
    finally:
        LOCK.unlink(missing_ok=True)
