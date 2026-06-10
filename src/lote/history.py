"""Command history for the ``lote`` CLI, in the shared ``.lote/db.sqlite``.

One ``history`` row per subcommand invocation (a :class:`HistoryEvent`), written with its
outcome and wall-clock duration -- the local audit trail ``lote history`` reads. Recording
is disabled by ``LOTE_NO_HISTORY=1``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path

import pendulum

from . import NAME, STATE_DIR
from .base import FrozenModel
from .storage import connect

DB_FILE = Path(STATE_DIR) / "db.sqlite"

# A positional argument as fire hands it to a subcommand: the scalar literals fire
# parses from the command line. History only stringifies them and picks the first
# string as the target, so the concrete union is all it needs.
type CommandArg = str | int | float | bool | None


class HistoryEvent(FrozenModel):
    """One recorded ``lote`` subcommand invocation.

    at: ISO-8601 timestamp (seconds) of when the command finished.
    command: the subcommand name (``ls``, ``submit``, ...).
    args: positional arguments the command was called with.
    target: the target alias the command acted on, when applicable.
    handle: the run handle produced or addressed, when applicable.
    outcome: ``ok`` if the command returned, ``error`` if it raised.
    detail: a short human note (e.g. the exception summary on error).
    duration_ms: wall-clock time the command took, in milliseconds.
    """

    at: str
    command: str
    args: list[str] = []
    target: str | None = None
    handle: str | None = None
    outcome: str
    detail: str | None = None
    duration_ms: int | None = None


class History:
    """The ``history`` table of the shared ``.lote/db.json`` log.

    Owns event construction so the CLI just calls :meth:`record`. Opt out with
    ``LOTE_NO_HISTORY=1`` (then :meth:`record` is a no-op).
    """

    def __init__(self, path: Path = DB_FILE) -> None:
        self.path = path
        self.enabled = os.environ.get(f"{NAME.upper()}_NO_HISTORY") != "1"
        self.db = connect(path) if self.enabled else None

    def record(
        self,
        command: str,
        args: Sequence[CommandArg],
        started: float,
        outcome: str,
        *,
        handle: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Append one timed invocation; the target is the first string argument."""
        if self.db is None:
            return
        event = HistoryEvent(
            at=pendulum.now().to_iso8601_string(),
            command=command,
            args=[str(arg) for arg in args],
            target=next((arg for arg in args if isinstance(arg, str)), None),
            handle=handle,
            outcome=outcome,
            detail=detail,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self.db.execute("INSERT INTO history (data) VALUES (?)", (event.model_dump_json(),))

    def recent(self, limit: int = 20) -> list[HistoryEvent]:
        """The last ``limit`` recorded events, oldest-to-newest, or [] if none."""
        if self.db is None:
            return []
        rows = self.db.execute(
            "SELECT data FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [HistoryEvent.model_validate_json(row["data"]) for row in reversed(rows)]
