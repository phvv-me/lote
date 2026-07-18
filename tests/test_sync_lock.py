from multiprocessing import get_context
from pathlib import Path
from time import monotonic, sleep

import pytest

import lote.sync as sync
from lote.sync import SyncLock


def acquire(root: str, target: str, waiting: str, acquired: str) -> None:
    """Mark entry to the lock call, then record when the target lock is acquired."""
    Path(waiting).touch()
    with SyncLock(target, Path(root)):
        Path(acquired).touch()


def wait_for(path: Path, timeout: float = 5) -> None:
    """Wait for a child-process marker without leaving an unbounded test."""
    deadline = monotonic() + timeout
    while not path.exists():
        if monotonic() >= deadline:
            raise TimeoutError(path)
        sleep(0.01)


def test_sync_lock_serializes_processes_per_target(tmp_path: Path) -> None:
    waiting = tmp_path / "waiting"
    acquired = tmp_path / "acquired"
    other_waiting = tmp_path / "other-waiting"
    other_acquired = tmp_path / "other-acquired"
    context = get_context("spawn")

    with SyncLock("crimson", tmp_path):
        process = context.Process(
            target=acquire,
            args=(str(tmp_path), "crimson", str(waiting), str(acquired)),
        )
        process.start()
        wait_for(waiting)
        process.join(0.2)
        assert process.is_alive()
        assert not acquired.exists()

        other = context.Process(
            target=acquire,
            args=(str(tmp_path), "gold", str(other_waiting), str(other_acquired)),
        )
        other.start()
        other.join(5)
        assert other.exitcode == 0
        assert other_acquired.exists()

    process.join(5)
    assert process.exitcode == 0
    assert acquired.exists()


def test_sync_lock_closes_its_file_when_acquisition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = (tmp_path / "file").open("a+")

    def fail(fileno: int, operation: int) -> None:
        del fileno, operation
        raise OSError("lock unavailable")

    monkeypatch.setattr(Path, "open", lambda self, mode: file)
    monkeypatch.setattr(sync.fcntl, "flock", fail)

    with pytest.raises(OSError, match="lock unavailable"):
        SyncLock("crimson", tmp_path).__enter__()
    assert file.closed
