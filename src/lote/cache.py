"""lote's state file: ``.lote/db.sqlite`` (a WAL-mode SQLite store).

Two keyed tables -- ``hosts`` (cached ssh-discovery facts, by alias) and ``runs`` (the
dispatched-job registry with provenance, by handle). Each row's flexible payload is a JSON
blob so the schema stays loose; SQLite gives concurrent-safe upserts (no whole-file rewrite,
no corruption) where the old TinyDB store needed a self-healing layer.
"""

from pathlib import Path

import pendulum

from . import STATE_DIR
from .base import FrozenModel
from .models import Target
from .storage import connect

DB_FILE = Path(STATE_DIR) / "db.sqlite"


class RunRecord(FrozenModel):
    """One dispatched job's provenance, the ``runs`` table row payload.

    handle: the scheduler's job handle (the lote-wide run id).
    target: the alias the job was dispatched to.
    kind: the target's scheduler kind at submit time (``ssh`` / ``pbs`` / ``slurm``).
    script: the job script path on the host.
    args: the script arguments, shell-quoted and space-joined.
    git_sha: the short HEAD sha the repo was at when dispatched.
    dirty: 1 when the working tree had uncommitted changes, else 0.
    submitted_at: ISO-8601 dispatch time.
    fetch_path: the results path to ``lote pull`` back, when ``--fetch`` was given.
    """

    handle: str
    target: str
    kind: str
    script: str
    args: str
    git_sha: str
    dirty: int
    submitted_at: str
    fetch_path: str | None = None


class Cache:
    """Lote state in one SQLite file with ``hosts`` and ``runs`` tables."""

    def __init__(self, path: Path = DB_FILE) -> None:
        self.path = path
        self.db = connect(path)

    def facts(self, alias: str) -> Target | None:
        """Cached discovery facts for ``alias`` as a Target, or None if never probed."""
        row = self.db.execute("SELECT facts FROM hosts WHERE alias = ?", (alias,)).fetchone()
        return Target.model_validate_json(row["facts"]) if row else None

    def save_facts(self, alias: str, facts: Target) -> None:
        """Cache discovery facts for ``alias`` (upsert by alias)."""
        self.db.execute(
            "INSERT INTO hosts (alias, facts, probed_at) VALUES (?, ?, ?) ON CONFLICT(alias) "
            "DO UPDATE SET facts = excluded.facts, probed_at = excluded.probed_at",
            (alias, facts.model_dump_json(), pendulum.now().to_iso8601_string()),
        )

    def record(self, run: RunRecord) -> None:
        """Record a dispatched run (upsert by ``handle``)."""
        self.db.execute(
            "INSERT INTO runs (handle, data, submitted_at) VALUES (?, ?, ?) ON CONFLICT(handle) "
            "DO UPDATE SET data = excluded.data, submitted_at = excluded.submitted_at",
            (run.handle, run.model_dump_json(), run.submitted_at),
        )

    def recent(self, limit: int = 20) -> list[RunRecord]:
        """The most recent dispatched runs, newest first."""
        rows = self.db.execute(
            "SELECT data FROM runs ORDER BY submitted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [RunRecord.model_validate_json(row["data"]) for row in rows]

    def run(self, handle: str) -> RunRecord:
        """One run by handle; an unknown handle raises a ``LookupError`` (the data
        layer's miss, translated to a user-facing exit at the CLI boundary)."""
        row = self.db.execute("SELECT data FROM runs WHERE handle = ?", (handle,)).fetchone()
        if row is None:
            raise LookupError(f"no recorded run {handle!r}")
        return RunRecord.model_validate_json(row["data"])
