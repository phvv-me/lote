from __future__ import annotations

import pytest
from rich.console import Console
from syrupy.assertion import SnapshotAssertion

from fleet.clients.pbs import JobInfo, JobState
from fleet.clients.pueue.state import PueueState
from fleet.clients.pueue.task import PueueTask
from fleet.clients.slurm import SlurmJob, SlurmState
from fleet.executor.cli import _print_jobs_table, _print_slurm_table
from fleet.history import HistoryEvent
from fleet.models import Target
from fleet.reconcile import ReconcileRow
from fleet.render import Renderer


def captured(console: Console) -> str:
    """The console's recorded output as text."""
    return console.export_text()


@pytest.fixture
def recorder() -> Console:
    """A capture console pinned to 80 columns so the table layout snapshots stably."""
    return Console(width=80, record=True, color_system=None, legacy_windows=False)


def test_pbs_jobs_table_snapshot(recorder: Console, snapshot: SnapshotAssertion) -> None:
    """The PBS status table layout is pinned for a running + queued job."""
    jobs = [
        JobInfo(
            job_id="123.pbs",
            name="train",
            user="a",
            state=JobState.RUNNING,
            queue="gpu",
            walltime="02:00:00",
            walltime_used="00:10:00",
        ),
        JobInfo(job_id="456.pbs", name="eval", user="a", state="X", queue="gen-S"),
    ]
    _print_jobs_table(jobs, console=recorder)
    assert captured(recorder) == snapshot


def test_slurm_jobs_table_snapshot(recorder: Console, snapshot: SnapshotAssertion) -> None:
    """The SLURM status table layout is pinned across a known and an unknown state."""
    jobs = [
        SlurmJob(
            job_id="1", name="train", state=SlurmState.RUNNING, partition="gpu", elapsed="00:05:00"
        ),
        SlurmJob(job_id="2", name="eval", state="ODD", partition=None, elapsed=None),
    ]
    _print_slurm_table(jobs, console=recorder)
    assert captured(recorder) == snapshot


def test_renderer_targets_snapshot(snapshot: SnapshotAssertion) -> None:
    """The `ls` view renders probed targets and tags an unprobed host."""
    renderer = Renderer()
    renderer.console = Console(width=80, record=True, color_system=None, legacy_windows=False)
    renderer.targets(
        [
            (
                "dgx",
                Target(
                    name="dgx",
                    kind="ssh",
                    gpu_name="NVIDIA GB10",
                    gpu_mem_mb=122880,
                    root="/work/projects",
                ),
            ),
            ("hpc", Target(name="hpc", kind="pbs", sysmem_gb=512, root="~/projects")),
            ("cold", None),
        ]
    )
    assert renderer.console.export_text() == snapshot


def test_renderer_tasks_snapshot(snapshot: SnapshotAssertion) -> None:
    """The pueue task table pins state colour-by-name and the result(code) cell."""
    renderer = Renderer()
    renderer.console = Console(width=80, record=True, color_system=None, legacy_windows=False)
    renderer.tasks(
        [
            PueueTask(id=1, label="train", state=PueueState.RUNNING, start="2024-01-01T12:00:00Z"),
            PueueTask(id=2, label=None, state=PueueState.DONE, result="Failed", exit_code=2),
        ]
    )
    assert renderer.console.export_text() == snapshot


def test_renderer_reconcile_snapshot(snapshot: SnapshotAssertion) -> None:
    """The reconcile table pins each verdict's row across the four outcomes."""
    renderer = Renderer()
    renderer.console = Console(width=80, record=True, color_system=None, legacy_windows=False)
    renderer.reconcile(
        [
            ReconcileRow(
                handle="1", script="a.sh", submitted_at="t1", state="F", exit_code=0, verdict="ok"
            ),
            ReconcileRow(
                handle="2", script="b.sh", submitted_at="t2", state="R", verdict="running"
            ),
            ReconcileRow(
                handle="3", script="c.sh", submitted_at="t3", state=None, verdict="vanished"
            ),
        ]
    )
    assert renderer.console.export_text() == snapshot


def test_renderer_runs_snapshot(snapshot: SnapshotAssertion) -> None:
    """The `ps` table pins the dirty marker on the git sha."""
    renderer = Renderer()
    renderer.console = Console(width=80, record=True, color_system=None, legacy_windows=False)
    renderer.runs(
        [
            {
                "submitted_at": "t1",
                "target": "dgx",
                "handle": "1",
                "script": "a.sh",
                "git_sha": "abc",
                "dirty": 0,
            },
            {
                "submitted_at": "t2",
                "target": "hpc",
                "handle": "2",
                "script": "b.sh",
                "git_sha": "def",
                "dirty": 1,
            },
        ]
    )
    assert renderer.console.export_text() == snapshot


def test_renderer_history_snapshot(snapshot: SnapshotAssertion) -> None:
    """The history table pins the ok/error colouring and the assembled command cell."""
    renderer = Renderer()
    renderer.console = Console(width=80, record=True, color_system=None, legacy_windows=False)
    renderer.history(
        [
            HistoryEvent(
                at="t1",
                command="submit",
                args=["dgx", "a.sh"],
                target="dgx",
                outcome="ok",
                duration_ms=12,
            ),
            HistoryEvent(
                at="t2",
                command="status",
                args=["hpc"],
                target="hpc",
                outcome="error",
                detail="boom",
            ),
        ]
    )
    assert renderer.console.export_text() == snapshot


@pytest.mark.parametrize(
    "render",
    [
        lambda r: r.runs([]),
        lambda r: r.tasks([]),
        lambda r: r.reconcile([]),
        lambda r: r.history([]),
    ],
)
def test_renderer_empty_states(render) -> None:  # noqa: ANN001
    """Every table renders a friendly placeholder rather than an empty frame when given nothing."""
    renderer = Renderer()
    renderer.console = Console(width=80, record=True, color_system=None, legacy_windows=False)
    render(renderer)
    assert "(no " in renderer.console.export_text()
