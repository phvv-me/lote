"""lote's state store: one WAL-mode SQLite file (``.lote/db.sqlite``).

Replaces the former TinyDB JSON document store, whose whole-file read-modify-write rewrite
had no locking -- two lote processes writing at once (overlapping ``run`` / ``submit``)
could truncate it, so a self-healing layer was needed. SQLite in WAL mode is concurrent-safe
by construction: readers never block, writes serialize with a busy timeout, and each upsert
is atomic, so the store cannot corrupt and no repair is needed. Rows keep their flexible
shape as JSON blobs (the state -- host facts, the run registry, the history log -- is all
regenerable, so the schema stays loose).
"""

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (alias TEXT PRIMARY KEY, facts TEXT NOT NULL, probed_at TEXT);
CREATE TABLE IF NOT EXISTS nodes (alias TEXT NOT NULL, class TEXT NOT NULL, facts TEXT NOT NULL,
    probed_at TEXT, PRIMARY KEY (alias, class));
CREATE TABLE IF NOT EXISTS runs (handle TEXT PRIMARY KEY, data TEXT NOT NULL, submitted_at TEXT);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the state database in WAL autocommit mode, creating the schema on first use.

    WAL lets concurrent lote commands read without blocking and serialize writes safely;
    ``busy_timeout`` retries a locked write rather than failing. Autocommit keeps each
    upsert/insert a single atomic statement.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10.0, autocommit=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript(_SCHEMA)
    return db
