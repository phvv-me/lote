import pytest
from hypothesis import given

from lote.models import Snapshot
from lote.nodes import (
    PROBE_COMMAND,
    PROBE_WALLTIME,
    parse_snapshot,
    probe_spec,
    wait_for,
)
from lote.schedulers import JobState

from .conftest import FakeRemote, RecordingScheduler
from .strategies import snapshots

H100_SNAPSHOT = Snapshot.model_validate(
    {
        "hostname": "node001",
        "cpu": {"name": "Grace", "logical_cores": 72},
        "memory": {"total_bytes": 100 * 1024**3},
        "gpus": [{"unit_name": "NVIDIA H100", "memory": {"total_bytes": 96 * 1024**3}}],
    }
)


# --- probe_spec ---


def test_probe_spec_is_the_minimal_mainboard_job() -> None:
    """The probe job runs only the mainboard dump on one node, no GPUs requested."""
    spec = probe_spec("debug-g")
    assert spec.cmd == PROBE_COMMAND
    assert spec.queue == "debug-g"
    assert spec.walltime == PROBE_WALLTIME
    assert spec.select == 1 and spec.gpus == 0


def test_probe_spec_renders_for_pbs_and_bash() -> None:
    """The rendered script carries the queue header on PBS and the dump everywhere."""
    pbs = probe_spec("prepost").render(pbs=True)
    bash = probe_spec("gpu").render(pbs=False)
    assert "#PBS -q prepost" in pbs
    assert "from mainboard import Machine" in pbs
    assert "#PBS" not in bash
    assert "from mainboard import Machine" in bash


# --- parse_snapshot ---


@given(snapshots())
def test_parse_snapshot_reads_the_json_line_through_noise(snapshot: Snapshot) -> None:
    """Activation noise around the snapshot line never confuses the parse."""
    log = f"module load cuda\nLoading modules...\n{snapshot.model_dump_json()}\ndone\n"
    assert parse_snapshot("debug-g", log) == snapshot.node_class("debug-g")


def test_parse_snapshot_last_json_line_wins() -> None:
    """With several JSON lines the snapshot printed last (most recent) is the one parsed."""
    log = '{"hostname": "older"}\n' + H100_SNAPSHOT.model_dump_json() + "\n"
    assert parse_snapshot("regular-g", log).hostname == "node001"


def test_parse_snapshot_skips_broken_json_lines() -> None:
    """A truncated JSON tail (a killed job) falls back to the previous parsable line."""
    log = H100_SNAPSHOT.model_dump_json() + '\n{"hostname": "trunc...\n'
    assert parse_snapshot("regular-g", log).gpu_name == "NVIDIA H100"


def test_parse_snapshot_without_snapshot_raises() -> None:
    """A log with no JSON object line is a LookupError naming the queue."""
    with pytest.raises(LookupError, match="debug-g"):
        parse_snapshot("debug-g", "Traceback (most recent call last):\nModuleNotFoundError\n")


# --- wait_for ---


def test_wait_for_returns_immediately_when_terminal() -> None:
    """A job already terminal returns its state with no sleeping and no cancel."""
    scheduler = RecordingScheduler()
    slept: list[float] = []
    final = wait_for(scheduler, FakeRemote(), "/repo", "H1", timeout=10.0, sleeper=slept.append)
    assert final.verdict == "ok"
    assert slept == []
    assert all(name != "cancel" for name, _ in scheduler.calls)


class SequencedScheduler(RecordingScheduler):
    """A scheduler double whose `state` replays a fixed sequence of JobStates."""

    def __init__(self, *states: JobState) -> None:
        super().__init__()
        self.pending = list(states)

    def state(self, remote, root, handle) -> JobState:  # noqa: ANN001
        self.calls.append(("state", (root, handle)))
        return self.pending.pop(0)


def test_wait_for_polls_until_terminal() -> None:
    """A running job is polled (with sleeps) until its verdict leaves `running`."""
    scheduler = SequencedScheduler(
        JobState(handle="H1", state="R", verdict="running"),
        JobState(handle="H1", state="F", exit_code=0, verdict="ok"),
    )
    slept: list[float] = []
    final = wait_for(scheduler, FakeRemote(), "/repo", "H1", timeout=100.0, sleeper=slept.append)
    assert final.verdict == "ok"
    assert len(slept) == 1


def test_wait_for_deadline_cancels_and_reports_timeout() -> None:
    """A job stuck in the queue past the deadline is cancelled and verdicted `timeout`."""
    scheduler = RecordingScheduler()
    scheduler.state_result = JobState(handle="H1", state="Q", verdict="running")
    ticks = iter([0.0, 100.0, 200.0])
    slept: list[float] = []
    final = wait_for(
        scheduler,
        FakeRemote(),
        "/repo",
        "H1",
        timeout=150.0,
        sleeper=slept.append,
        clock=lambda: next(ticks),
    )
    assert final.verdict == "timeout"
    assert final.state == "Q"
    assert ("cancel", ("/repo", "H1")) in scheduler.calls
    assert len(slept) == 1  # one sleep before the deadline hit
