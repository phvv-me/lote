"""The lote's single state file: ``.lote/db.json`` (a TinyDB document store).

Two keyed tables — ``hosts`` (cached ssh-discovery facts, by alias) and
``runs`` (the dispatched-job registry with provenance, by handle). It's a few
hundred rows written by one CLI process, so TinyDB's human-readable JSON beats a
server database: a single dependency, inspectable with ``cat``, with upserts
that keep each alias/handle unique.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pendulum
from tinydb import Query, TinyDB

from . import STATE_DIR

DB_FILE = Path(STATE_DIR) / "db.json"


class Cache:
    """Lote state held in one TinyDB file with ``hosts`` and ``runs`` tables."""

    def __init__(self, path: Path = DB_FILE) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = TinyDB(path)
        self.hosts = self.db.table("hosts")
        self.runs = self.db.table("runs")

    def facts(self, alias: str) -> dict[str, Any] | None:
        """Cached discovery facts for ``alias``, or None if never probed."""
        host = self.hosts.get(Query().alias == alias)
        return host["facts"] if isinstance(host, dict) else None

    def save_facts(self, alias: str, facts: dict[str, Any]) -> None:
        """Cache discovery facts for ``alias`` (upsert by alias)."""
        self.hosts.upsert(
            {"alias": alias, "facts": facts, "probed_at": pendulum.now().to_iso8601_string()},
            Query().alias == alias,
        )

    def record(self, run: dict[str, Any]) -> None:
        """Record a dispatched run (upsert by ``handle``)."""
        self.runs.upsert(run, Query().handle == run["handle"])

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """The most recent dispatched runs, newest first."""
        runs = sorted(self.runs.all(), key=lambda run: run["submitted_at"], reverse=True)
        return [dict(run) for run in runs[:limit]]

    def run(self, handle: str) -> dict[str, Any]:
        """One run by handle (raises if absent)."""
        run = self.runs.get(Query().handle == handle)
        if not isinstance(run, dict):
            raise SystemExit(f"no recorded run {handle!r}")
        return dict(run)
