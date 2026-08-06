"""The single-watcher lock caps concurrent ``lote wait`` at one, so several backgrounded
watchers can never exhaust a host's SSH ``MaxSessions``. A second watcher must refuse loudly,
and a lock left by a watcher that died must be reclaimed so a crash never wedges the slot.
"""

import os

import pytest

import lote.watcher as watcher
from lote.watcher import LOCK, alive, holder, single_watcher


def test_lock_starts_free_and_acquires(workdir):
    """With no prior watcher the slot is free, and entering claims it with this pid and handle."""
    assert holder() is None
    with single_watcher("job-A"):
        owner = holder()
        assert owner == (os.getpid(), "job-A")


def test_lock_releases_on_exit(workdir):
    """Leaving the context frees the slot so the next watcher can take it."""
    with single_watcher("job-A"):
        pass
    assert holder() is None
    assert not LOCK.exists()


def test_second_watcher_is_refused(workdir):
    """A second watcher while one is live exits with a message naming the held handle and pid."""
    with single_watcher("job-A"), pytest.raises(SystemExit) as caught, single_watcher("job-B"):
        pass
    message = str(caught.value)
    assert "job-A" in message
    assert str(os.getpid()) in message


def test_stale_lock_from_a_dead_watcher_is_reclaimed(workdir):
    """A lock stamped with a pid no longer running is treated as free and overwritten."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text("999999 ghost")  # a pid that is not running
    assert holder() is None
    with single_watcher("job-A"):
        assert holder() == (os.getpid(), "job-A")


def test_alive_reports_the_current_process(workdir):
    """``alive`` is true for our own pid and false for an unused one."""
    assert alive(os.getpid())
    assert not alive(999999)


def test_alive_counts_a_process_we_may_not_signal(workdir):
    """pid 1 (init, owned by root) counts as alive though signalling it raises PermissionError."""
    assert alive(1)


def test_alive_counts_permission_denied_as_live(monkeypatch, workdir):
    """A process that rejects the signal still counts as live."""

    def deny_signal(pid: int, signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(watcher.os, "kill", deny_signal)
    assert alive(123)
