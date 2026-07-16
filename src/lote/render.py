"""All rich/console rendering for the ``lote`` CLI in one place.

The CLI owns a :class:`Renderer` and hands it plain data — resolved
:class:`Target` objects, recorded run dicts, pueue tasks, history events,
reconcile rows — so dispatch logic never touches ``rich`` directly. Keeping
every ``Table``/``Console`` call here means the CLI reads as orchestration and
the look of the output can change without touching command code.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import pendulum
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.status import Status
from rich.table import Table

from .clients import pueue


def when(submitted_at: str) -> str:
    """A human relative age (``5 minutes ago``) for a run's ISO submit time, or the raw value."""
    try:
        return pendulum.parse(submitted_at).diff_for_humans()
    except ValueError:
        return submitted_at or "-"


if TYPE_CHECKING:
    from .cache import RunRecord
    from .history import HistoryEvent
    from .models import Target
    from .reconcile import ReconcileRow
    from .schedulers import JobState
    from .services import ServiceStatus


# pueue lifecycle state -> table colour.
PUEUE_PALETTE: dict[str, str] = {
    pueue.PueueState.RUNNING: "green",
    pueue.PueueState.QUEUED: "yellow",
    pueue.PueueState.PAUSED: "magenta",
    pueue.PueueState.DONE: "blue",
}

# reconcile verdict -> table colour.
VERDICT_PALETTE: dict[str, str] = {
    "ok": "green",
    "running": "cyan",
    "failed": "red",
    "vanished": "yellow",
    "unreachable": "magenta",
}


def vram_label(vram: float | None) -> str:
    """A memory cell like ``120GB``, or ``?`` while a class is unprobed."""
    return f"{vram:.0f}GB" if vram else "?"


class Renderer:
    """Turns lote data structures into rich console tables."""

    def __init__(self) -> None:
        self.console = Console()

    def targets(self, targets: list[tuple[str, Target | None]]) -> None:
        """Print the ``ls`` view: one line per target, plus its node classes when probed.

        Each row is ``(alias, target_or_None)``; an unprobed host (``None``) shows
        just its name tagged ``(not probed)`` rather than fabricated capabilities.
        The alias line carries the most capable class; a host with classes beyond
        ``login`` also lists each class beneath it.
        """
        for alias, target in targets:
            if target is None:
                self.console.print(f"{alias:12} [dim](not probed)[/dim]")
                continue
            self.console.print(
                f"{alias:12} {target.kind:4} {target.arch or '-':7} "
                f"{vram_label(target.vram_gb):>6}  {target.root}"
            )
            if len(target.classes) > 1:
                for name, node in sorted(target.classes.items()):
                    gpus = f"  x{node.gpu_count}" if node.gpu_count else ""
                    self.console.print(
                        f"  [dim]{name:15} {node.arch or '-':7} "
                        f"{vram_label(node.vram_gb):>6}{gpus}[/dim]"
                    )

    def runs(self, runs: list[RunRecord]) -> None:
        """Print the ``ps`` table: recent dispatched runs across all targets."""
        if not runs:
            self.console.print("(no runs recorded)")
            return
        table = Table(show_header=True, header_style="bold cyan")
        for column in ("when", "target", "handle", "script", "code"):
            table.add_column(column)
        for run in runs:
            table.add_row(
                run.submitted_at,
                run.target,
                run.handle,
                run.script,
                run.git_sha + ("+" if run.dirty else ""),
            )
        self.console.print(table)

    def states(self, target: str, states: list[JobState], *, verbose: bool = False) -> None:
        """Print one host's live scheduler jobs (the ``status <target>`` table), uniformly.

        Each row is a backend-agnostic :class:`JobState`, so pueue tasks, PBS jobs, and SLURM jobs
        render the same columns colored by the normalized ``status`` (verdict); ``verbose`` adds
        the scheduler's own raw ``state`` code beside it.
        """
        if not states:
            self.console.print(f"(no live jobs on {target})")
            return
        table = Table(show_header=True, header_style="bold cyan")
        columns = ["id", "name", *(["state"] if verbose else []), "status"]
        for column in columns:
            table.add_column(column)
        for state in states:
            color = VERDICT_PALETTE.get(state.verdict, "white")
            cells = [state.handle, state.label or "-"]
            if verbose:
                cells.append(state.state or "-")
            cells.append(f"[{color}]{state.verdict}[/{color}]")
            table.add_row(*cells)
        self.console.print(table)

    def tasks(self, tasks: list[pueue.PueueTask]) -> None:
        """Print the pueue-task table used by ``status``/``info`` on ssh targets."""
        if not tasks:
            self.console.print("(no tasks)")
            return
        table = Table(show_header=True, header_style="bold cyan")
        for column in ("id", "label", "state", "result", "start"):
            table.add_column(column)
        for task in tasks:
            color = PUEUE_PALETTE.get(task.state, "white")
            result = f"{task.result}({task.exit_code})" if task.exit_code else (task.result or "-")
            table.add_row(
                str(task.id),
                task.label or "-",
                f"[{color}]{task.state}[/{color}]",
                result,
                (task.start or "-")[11:19],
            )
        self.console.print(table)

    def history(self, events: list[HistoryEvent]) -> None:
        """Print recent command history (the ``lote history`` view)."""
        if not events:
            self.console.print("(no history)")
            return
        table = Table(show_header=True, header_style="bold cyan")
        for column in ("when", "command", "target", "outcome", "ms", "detail"):
            table.add_column(column)
        for event in events:
            color = "green" if event.outcome == "ok" else "red"
            table.add_row(
                event.at,
                " ".join([event.command, *event.args]).strip(),
                event.target or "-",
                f"[{color}]{event.outcome}[/{color}]",
                str(event.duration_ms) if event.duration_ms is not None else "-",
                event.detail or "-",
            )
        self.console.print(table)

    def reconcile(self, rows: list[ReconcileRow]) -> None:
        """Print the ``reconcile`` table: local run state vs the live scheduler."""
        if not rows:
            self.console.print("(no runs to reconcile)")
            return
        table = Table(show_header=True, header_style="bold cyan")
        for column in ("handle", "script", "submitted", "state", "exit", "verdict"):
            table.add_column(column)
        for row in rows:
            color = VERDICT_PALETTE.get(row.verdict, "white")
            table.add_row(
                row.handle,
                row.script,
                row.submitted_at,
                row.state or "-",
                str(row.exit_code) if row.exit_code is not None else "-",
                f"[{color}]{row.verdict}[/{color}]",
            )
        self.console.print(table)

    def jobs(self, rows: list[tuple[str, ReconcileRow]], *, verbose: bool = False) -> None:
        """One unified table of jobs across every target (the no-arg ``status`` view)."""
        self.console.print(self._jobs_table(rows, verbose=verbose))

    def _jobs_table(
        self, rows: list[tuple[str, ReconcileRow]], *, verbose: bool = False
    ) -> RenderableType:
        """The cross-target jobs table renderable (shared by ``status`` and ``monitor``).

        The ``status`` column is the normalized verdict; ``verbose`` inserts the scheduler's own
        raw ``state`` code beside it, otherwise that backend-specific detail stays hidden.
        """
        if not rows:
            return "(no jobs across targets)"
        # newest first across every host: ISO-8601 submit times sort chronologically as strings, so
        # the freshest run is always on top regardless of which target it ran on.
        rows = sorted(rows, key=lambda pair: pair[1].submitted_at, reverse=True)
        table = Table(show_header=True, header_style="bold cyan")
        columns = ["target", "id", "name", "age"]
        if verbose:
            columns.append("state")
        columns.append("status")
        for column in columns:
            table.add_column(column)
        for target, run in rows:
            color = VERDICT_PALETTE.get(run.verdict, "white")
            cells = [target, run.handle, run.name or Path(run.script).name, when(run.submitted_at)]
            if verbose:
                cells.append(run.state or "-")
            cells.append(f"[{color}]{run.verdict}[/{color}]")
            table.add_row(*cells)
        return table

    def services(self, statuses: list[ServiceStatus]) -> None:
        """Print the ``serve status`` table: one row per service, health colored."""
        if not statuses:
            self.console.print("(no services)")
            return
        table = Table(show_header=True, header_style="bold cyan")
        for column in ("name", "target", "local", "remote task", "tunnel task", "since", "health"):
            table.add_column(column)
        for item in statuses:
            record = item.record
            color = "green" if item.healthy else "yellow"
            health = "healthy" if item.healthy else "unhealthy"
            table.add_row(
                record.name,
                record.target,
                f"localhost:{record.local_port}",
                record.remote_task,
                record.tunnel_task,
                when(record.started_at),
                f"[{color}]{health}[/{color}]",
            )
        self.console.print(table)

    def live(self) -> Live:
        """A rich ``Live`` bound to this console for ``monitor``'s refresh-in-place loop."""
        return Live(console=self.console, refresh_per_second=4, transient=False)

    def spinner(self, message: str) -> Status:
        """A transient spinner (``with ... as status: status.update(...)``) for slow multi-host
        work, so ``status`` shows which host it is probing instead of hanging silently."""
        return self.console.status(message, spinner="dots")

    def monitor(
        self,
        jobs: list[tuple[str, ReconcileRow]],
        progress: int | None,
        *,
        path: str | None,
    ) -> RenderableType:
        """The combined ``monitor`` renderable: the jobs table plus a progress line.

        jobs: ``(alias, row)`` pairs for every live run, as ``status`` builds.
        progress: total ``part-*.parquet`` shards fetched so far, or None when no
            ``--fetch`` path was given.
        path: the results path being tallied, shown in the progress caption.
        """
        renderables: list[RenderableType] = [self._jobs_table(jobs)]
        if progress is not None:
            renderables.append(
                Panel(
                    f"{progress} part-*.parquet shard(s) under [bold]{path}[/bold]",
                    title="progress",
                    border_style="cyan",
                )
            )
        return Group(*renderables)
