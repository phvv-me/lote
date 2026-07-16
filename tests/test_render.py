from collections.abc import Callable

import pytest
from rich.console import Console
from syrupy.assertion import SnapshotAssertion

from lote.cache import RunRecord
from lote.clients.pbs import JobInfo, PbsState
from lote.clients.pueue.state import PueueState
from lote.clients.pueue.task import PueueTask
from lote.clients.slurm import SlurmJob, SlurmState
from lote.executor.cli import _print_jobs_table, _print_slurm_table
from lote.history import HistoryEvent
from lote.models import LOGIN, NodeClass, Target
from lote.reconcile import ReconcileRow
from lote.render import Renderer
from lote.schedulers import JobState as SchedulerJobState
from lote.services import ServiceStatus

from .conftest import make_service


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
            state=PbsState.RUNNING,
            queue="gpu",
            walltime="02:00:00",
            walltime_used="00:10:00",
        ),
        JobInfo(job_id="456.pbs", name="eval", user="a", state="X", queue="gen-S"),
    ]
    _print_jobs_table(jobs, console=recorder)
    assert recorder.export_text().rstrip() == snapshot


def test_slurm_jobs_table_snapshot(recorder: Console, snapshot: SnapshotAssertion) -> None:
    """The SLURM status table layout is pinned across a known and an unknown state."""
    jobs = [
        SlurmJob(
            job_id="1", name="train", state=SlurmState.RUNNING, partition="gpu", elapsed="00:05:00"
        ),
        SlurmJob(job_id="2", name="eval", state="ODD", partition=None, elapsed=None),
    ]
    _print_slurm_table(jobs, console=recorder)
    assert recorder.export_text().rstrip() == snapshot


def test_renderer_targets_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The `ls` view renders probed targets, their node classes, and tags an unprobed host."""
    renderer.targets(
        [
            (
                "dgx",
                Target(
                    name="dgx",
                    kind="ssh",
                    root="/work/projects",
                    classes={
                        LOGIN: NodeClass(name=LOGIN, gpu_name="NVIDIA GB10", gpu_mem_mb=122880)
                    },
                ),
            ),
            (
                "hpc",
                Target(
                    name="hpc",
                    kind="pbs",
                    root="~/projects",
                    classes={
                        LOGIN: NodeClass(name=LOGIN, sysmem_gb=512),
                        "debug-g": NodeClass(
                            name="debug-g",
                            gpu_name="NVIDIA H100",
                            gpu_count=1,
                            gpu_mem_mb=98304,
                        ),
                        "prepost": NodeClass(name="prepost", sysmem_gb=1024),
                    },
                ),
            ),
            ("cold", None),
        ]
    )
    assert renderer.console.export_text().rstrip() == snapshot


def test_renderer_tasks_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The pueue task table pins state colour-by-name and the result(code) cell."""
    renderer.tasks(
        [
            PueueTask(id=1, label="train", state=PueueState.RUNNING, start="2024-01-01T12:00:00Z"),
            PueueTask(id=2, label=None, state=PueueState.DONE, result="Failed", exit_code=2),
        ]
    )
    assert renderer.console.export_text().rstrip() == snapshot


def test_renderer_services_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The `serve status` table pins the healthy/unhealthy colour and the tunneled address."""
    renderer.services(
        [
            ServiceStatus(record=make_service("vllm", local_port=8000), healthy=True),
            ServiceStatus(record=make_service("stale", local_port=8001), healthy=False),
        ]
    )
    assert renderer.console.export_text().rstrip() == snapshot


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
    assert renderer.console.export_text().rstrip() == snapshot


def test_renderer_runs_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The `ps` table pins the dirty marker on the git sha."""
    renderer.runs(
        [
            RunRecord(
                submitted_at="t1",
                target="dgx",
                handle="1",
                kind="ssh",
                script="a.sh",
                args="",
                git_sha="abc",
                dirty=0,
            ),
            RunRecord(
                submitted_at="t2",
                target="hpc",
                handle="2",
                kind="ssh",
                script="b.sh",
                args="",
                git_sha="def",
                dirty=1,
            ),
        ]
    )
    assert renderer.console.export_text().rstrip() == snapshot


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
    assert renderer.console.export_text().rstrip() == snapshot


def test_renderer_jobs_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The unified cross-target jobs table pins the per-target rows and verdict colour."""
    renderer.jobs(
        [
            (
                "dgx",
                ReconcileRow(
                    handle="1", script="a.sh", submitted_at="t1", state="R", verdict="running"
                ),
            ),
            (
                "hpc",
                ReconcileRow(
                    handle="2", script="b.sh", submitted_at="t2", state=None, verdict="vanished"
                ),
            ),
        ]
    )
    assert renderer.console.export_text().rstrip() == snapshot


def test_renderer_jobs_table_sorts_newest_first(renderer: Renderer) -> None:
    """Rows render newest-first by submit time, across hosts, whatever order they arrive in."""
    renderer.jobs(
        [
            (
                "dgx",
                ReconcileRow(
                    handle="old", script="a.sh", submitted_at="2026-06-16T09:00:00Z", verdict="ok"
                ),
            ),
            (
                "hpc",
                ReconcileRow(
                    handle="new",
                    script="b.sh",
                    submitted_at="2026-06-16T11:00:00Z",
                    verdict="running",
                ),
            ),
        ]
    )
    text = renderer.console.export_text()
    assert text.index("new") < text.index("old")  # the fresher run sits above the older one


def test_renderer_states_snapshot(renderer: Renderer, snapshot: SnapshotAssertion) -> None:
    """The per-target live-jobs table pins one backend-agnostic JobState row per job."""
    renderer.states(
        "spark",
        [
            SchedulerJobState(handle="5", label="train", state="Running", verdict="running"),
            SchedulerJobState(handle="6", label=None, state="Done", verdict="ok"),
        ],
    )
    assert renderer.console.export_text().rstrip() == snapshot


def wide_renderer() -> Renderer:
    """A Renderer on a 200-column recording console, so a verbose table never truncates a cell."""
    instance = Renderer()
    instance.console = Console(width=200, record=True, color_system=None, legacy_windows=False)
    return instance


def test_renderer_states_verbose_shows_the_raw_scheduler_state() -> None:
    """`--verbose` inserts the backend's own state code beside the normalized verdict per job."""
    renderer = wide_renderer()
    renderer.states(
        "spark",
        [
            SchedulerJobState(handle="5", label="train", state="Running", verdict="running"),
            SchedulerJobState(handle="6", label=None, state=None, verdict="vanished"),
        ],
        verbose=True,
    )
    text = renderer.console.export_text()
    assert "Running" in text  # the raw state code rides beside the verdict
    assert "running" in text and "vanished" in text


def test_renderer_jobs_verbose_shows_state_and_paints_an_unreachable_host() -> None:
    """The cross-host table gains the raw-state column under `--verbose`, and paints `unreachable`.

    A row whose host could not be probed renders the new ``unreachable`` verdict (its reason in the
    state cell), proving the verdict palette covers it and one dead host never breaks the table.
    """
    renderer = wide_renderer()
    renderer.jobs(
        [
            (
                "dgx",
                ReconcileRow(
                    handle="1", script="a.sh", submitted_at="t1", state="R", verdict="running"
                ),
            ),
            (
                "gold",
                ReconcileRow(
                    handle="2",
                    script="b.sh",
                    submitted_at="t0",
                    state="daemon down",
                    verdict="unreachable",
                ),
            ),
            (
                "hpc",
                ReconcileRow(
                    handle="3", script="c.sh", submitted_at="t2", state=None, verdict="ok"
                ),
            ),
        ],
        verbose=True,
    )
    text = renderer.console.export_text()
    assert "unreachable" in text and "daemon down" in text
    assert "R" in text  # the raw-state column is present


@pytest.mark.parametrize(
    "render",
    [
        lambda r: r.runs([]),
        lambda r: r.tasks([]),
        lambda r: r.reconcile([]),
        lambda r: r.history([]),
        lambda r: r.jobs([]),
        lambda r: r.states("spark", []),
        lambda r: r.services([]),
    ],
)
def test_renderer_empty_states(renderer: Renderer, render: Callable[[Renderer], None]) -> None:
    """Every table renders a friendly placeholder rather than an empty frame when given nothing."""
    render(renderer)
    assert "(no " in renderer.console.export_text()


def test_renderer_live_binds_console(renderer: Renderer) -> None:
    """`live()` returns a Live bound to the renderer's console for in-place refresh."""
    live = renderer.live()
    assert live.console is renderer.console


def test_renderer_monitor_combines_jobs_and_progress(renderer: Renderer) -> None:
    """`monitor` stacks the cross-target jobs table above a progress panel when a path is set."""
    jobs = [
        ("dgx", ReconcileRow(handle="1", script="a.sh", submitted_at="t1", verdict="running")),
    ]
    renderer.console.print(renderer.monitor(jobs, progress=7, path="out/"))
    text = renderer.console.export_text()
    assert "dgx" in text and "running" in text  # the jobs table
    assert "7 part-*.parquet" in text and "out/" in text  # the progress panel


def test_renderer_monitor_without_path_omits_progress(renderer: Renderer) -> None:
    """With no fetch path the monitor renders only the jobs table, no progress panel."""
    renderer.console.print(renderer.monitor([], progress=None, path=None))
    text = renderer.console.export_text()
    assert "(no jobs across targets)" in text
    assert "parquet" not in text
