"""All rich/console rendering for the ``fleet`` CLI in one place.

The CLI owns a :class:`Renderer` and hands it plain data — resolved
:class:`Target` objects, recorded run dicts, pueue tasks, history events,
reconcile rows — so dispatch logic never touches ``rich`` directly. Keeping
every ``Table``/``Console`` call here means the CLI reads as orchestration and
the look of the output can change without touching command code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from .clients import pueue

if TYPE_CHECKING:
    from .history import HistoryEvent
    from .models import Target
    from .reconcile import ReconcileRow


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
}


class Renderer:
    """Turns fleet data structures into rich console tables."""

    def __init__(self) -> None:
        self.console = Console()

    def targets(self, targets: list[tuple[str, Target | None]]) -> None:
        """Print the ``ls`` view: one line per target (alias + cached facts).

        Each row is ``(alias, target_or_None)``; an unprobed host (``None``) shows
        just its name tagged ``(not probed)`` rather than fabricated capabilities.
        """
        for alias, target in targets:
            if target is None:
                self.console.print(f"{alias:12} [dim](not probed)[/dim]")
                continue
            vram = f"{target.vram_gb:.0f}GB" if target.vram_gb else "?"
            self.console.print(
                f"{alias:12} {target.kind:4} {target.arch or '-':7} {vram:>6}  {target.root}"
            )

    def runs(self, runs: list[dict[str, Any]]) -> None:
        """Print the ``ps`` table: recent dispatched runs across all targets."""
        if not runs:
            self.console.print("(no runs recorded)")
            return
        table = Table(show_header=True, header_style="bold cyan")
        for column in ("when", "target", "handle", "script", "code"):
            table.add_column(column)
        for run in runs:
            table.add_row(
                run["submitted_at"],
                run["target"],
                run["handle"],
                run["script"],
                run["git_sha"] + ("+" if run["dirty"] else ""),
            )
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
        """Print recent command history (the ``fleet history`` view)."""
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
