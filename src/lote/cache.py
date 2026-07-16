"""lote's state file: ``.lote/db.sqlite`` (a WAL-mode SQLite store).

Keyed tables -- ``hosts`` (cached ssh-discovery facts, by alias), ``nodes`` (one probed node
class per ``(alias, class)`` key, so each queue's capabilities cache independently), ``runs``
(the dispatched-job registry with provenance, by handle), and ``services`` (the persistent
``lote serve`` registry, by name). Each row's flexible payload is a JSON blob so the schema
stays loose; SQLite gives concurrent-safe upserts (no whole-file rewrite, no corruption)
where the old TinyDB store needed a self-healing layer.
"""

from pathlib import Path

import pendulum

from . import STATE_DIR
from .base import FrozenModel
from .models import NodeClass, Target
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
    # a human label for the run (``--name``), shown in ``status`` instead of the internal script
    # path; empty falls back to the script's basename at render time.
    name: str = ""
    # the last resolved scheduler outcome, memoized so a finished job (whose verdict can never
    # change) is rendered straight from the cache instead of re-probed over ssh on every status.
    # ``None`` means never resolved; a terminal verdict here is trusted without touching the host.
    state: str | None = None
    exit_code: int | None = None
    verdict: str | None = None
    # the verdict the durable monitor (``lote monitor --once``) last surfaced for this run, the
    # change cursor that keeps a periodic sweep reporting only jobs newly terminal since the last
    # check. ``None`` means never reported, so the first sweep that finds it terminal announces it.
    reported: str | None = None


class ServiceRecord(FrozenModel):
    """A persistent service ``lote serve`` launched, the ``services`` table row payload.

    A service never finishes on its own (a vLLM server, a notebook), so unlike a
    :class:`RunRecord` it is keyed by its human ``name`` rather than a scheduler handle, and
    it carries everything ``stop``/``status``/``logs`` need to act on it again without
    re-resolving the host.

    name: the service's user-chosen name, the key later ``serve`` commands look it up by.
    target: the ssh alias the service runs on.
    root: the target's repo root at launch time (only used to locate the chefe-env ``pueue``
        binary; the service itself is unrelated to the synced repo).
    cmd: the command that was launched.
    port: the port the service binds on ``target``.
    local_port: the local port ``stop``/``status`` reach it on, tunneled from ``port``.
    health_path: the HTTP path polled to decide the service is up.
    remote_task: the pueue task id supervising the service on ``target``.
    tunnel_task: the local pueue task id supervising the ``ssh -L`` tunnel.
    started_at: ISO-8601 launch time.
    """

    name: str
    target: str
    root: str
    cmd: str
    port: int
    local_port: int
    health_path: str
    remote_task: str
    tunnel_task: str
    started_at: str


class Cache:
    """Lote state in one SQLite file with ``hosts``, ``runs`` and ``services`` tables."""

    def __init__(self, path: Path = DB_FILE) -> None:
        self.path = path
        self.db = connect(path)

    def facts(self, alias: str) -> Target | None:
        """Cached facts for ``alias`` with its node classes attached, or None if never probed."""
        row = self.db.execute("SELECT facts FROM hosts WHERE alias = ?", (alias,)).fetchone()
        if row is None:
            return None
        target = Target.model_validate_json(row["facts"])
        return target.model_copy(update={"classes": self.classes(alias)})

    def classes(self, alias: str) -> dict[str, NodeClass]:
        """Every cached node class for ``alias``, keyed by class name."""
        rows = self.db.execute(
            "SELECT class, facts FROM nodes WHERE alias = ?", (alias,)
        ).fetchall()
        return {row["class"]: NodeClass.model_validate_json(row["facts"]) for row in rows}

    def save_facts(self, alias: str, facts: Target) -> None:
        """Cache discovery facts for ``alias``: the host row plus one row per node class.

        Classes live in their own table under ``(alias, class)`` keys; a full save
        replaces the alias's class rows so a queue retired from the cluster cannot
        linger in the cache.
        """
        host = facts.model_copy(update={"classes": {}})
        self.db.execute(
            "INSERT INTO hosts (alias, facts, probed_at) VALUES (?, ?, ?) ON CONFLICT(alias) "
            "DO UPDATE SET facts = excluded.facts, probed_at = excluded.probed_at",
            (alias, host.model_dump_json(), pendulum.now().to_iso8601_string()),
        )
        self.db.execute("DELETE FROM nodes WHERE alias = ?", (alias,))
        for node in facts.classes.values():
            self.save_node(alias, node)

    def save_node(self, alias: str, node: NodeClass) -> None:
        """Cache one node class for ``alias`` (upsert by the ``(alias, class)`` key)."""
        self.db.execute(
            "INSERT INTO nodes (alias, class, facts, probed_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(alias, class) DO UPDATE SET facts = excluded.facts, "
            "probed_at = excluded.probed_at",
            (alias, node.name, node.model_dump_json(), pendulum.now().to_iso8601_string()),
        )

    def record(self, run: RunRecord) -> None:
        """Record a dispatched run (upsert by ``handle``)."""
        self.db.execute(
            "INSERT INTO runs (handle, data, submitted_at) VALUES (?, ?, ?) ON CONFLICT(handle) "
            "DO UPDATE SET data = excluded.data, submitted_at = excluded.submitted_at",
            (run.handle, run.model_dump_json(), run.submitted_at),
        )

    def resolve(
        self, run: RunRecord, state: str | None, exit_code: int | None, verdict: str
    ) -> None:
        """Memoize a run's resolved scheduler outcome, so a terminal verdict is never re-probed.

        Writes the live state/exit/verdict back onto the run row (preserving its provenance and
        submit time), turning the next ``status`` into a cache read for any job that has finished.
        """
        fields = {"state": state, "exit_code": exit_code, "verdict": verdict}
        self.record(run.model_copy(update=fields))

    def report(self, run: RunRecord, verdict: str) -> None:
        """Record the verdict the durable monitor last surfaced for ``run``.

        The sweep's change cursor: a later ``lote monitor --once`` compares this against the fresh
        resolved verdict, so a job already announced terminal is not reported again.
        """
        self.record(run.model_copy(update={"reported": verdict}))

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

    def save_service(self, record: ServiceRecord) -> None:
        """Record a launched service (upsert by ``name``)."""
        self.db.execute(
            "INSERT INTO services (name, data) VALUES (?, ?) ON CONFLICT(name) "
            "DO UPDATE SET data = excluded.data",
            (record.name, record.model_dump_json()),
        )

    def service(self, name: str) -> ServiceRecord:
        """One service by name; an unknown name raises a ``LookupError``."""
        row = self.db.execute("SELECT data FROM services WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no service named {name!r}")
        return ServiceRecord.model_validate_json(row["data"])

    def services(self) -> list[ServiceRecord]:
        """Every recorded service, alphabetically by name."""
        rows = self.db.execute("SELECT data FROM services ORDER BY name").fetchall()
        return [ServiceRecord.model_validate_json(row["data"]) for row in rows]

    def remove_service(self, name: str) -> None:
        """Drop a service's record (after ``stop``); a no-op when it is already gone."""
        self.db.execute("DELETE FROM services WHERE name = ?", (name,))
