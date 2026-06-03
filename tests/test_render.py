from __future__ import annotations

from collections.abc import Callable

import pytest
from rich.console import Console
from syrupy.assertion import SnapshotAssertion

from lote.clients.pbs import JobInfo, JobState
from lote.clients.pueue.state import PueueState
from lote.clients.pueue.task import PueueTask
from lote.clients.slurm import SlurmJob, SlurmState
from lote.executor.cli import _print_jobs_table, _print_slurm_table
from lote.history import HistoryEvent
from lote.models import Target
from lote.reconcile import ReconcileRow
from lote.render import Renderer


@pytest.fixture
def renderer(recorder: Console) -> Renderer:
    """A Renderer whose console records to the shared 80-column capture buffer."""
    instance = Renderer()
    instance.console = recorder
    return instance


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
    assert recorder.export_text() == snapshot


def test_slurm_jobs_table_snapshot(recorder: Console, snapshot: SnapshotAssertion) -> None:
    """The SLURM status table layout is pinned across a known and an unknown state."""
    jobs = [
        SlurmJob(
            job_id="1", name="train", state=SlurmState.RUNNING, partition="gpu", elapsed="00:05:00"
        ),
        SlurmJob(job_id="2", name="eval", state="ODD", partition=None, elapsed=None),
    ]
    _print_slurm_table(jobs, console=recorder)
    assert recorder.export_text() == snapshot


def test_renderer_targets_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The `ls` view renders probed targets and tags an unprobed host."""
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


def test_renderer_tasks_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The pueue task table pins state colour-by-name and the result(code) cell."""
    renderer.tasks(
        [
            PueueTask(id=1, label="train", state=PueueState.RUNNING, start="2024-01-01T12:00:00Z"),
            PueueTask(id=2, label=None, state=PueueState.DONE, result="Failed", exit_code=2),
        ]
    )
    assert renderer.console.export_text() == snapshot


def test_renderer_reconcile_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The reconcile table pins each verdict's row across the four outcomes."""
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


def test_renderer_runs_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The `ps` table pins the dirty marker on the git sha."""
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


def test_renderer_history_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The history table pins the ok/error colouring and the assembled command cell."""
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
def test_renderer_empty_states(renderer: Renderer, render: Callable[[Renderer], None]) -> None:
    """Every table renders a friendly placeholder rather than an empty frame when given nothing."""
    render(renderer)
    assert "(no " in renderer.console.export_text()
