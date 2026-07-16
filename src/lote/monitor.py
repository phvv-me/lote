"""The durable single-pass monitor's report types and the verdict vocabulary it classifies on.

``lote monitor --once`` resolves every tracked job across all hosts once (robustly, so a dead host
never crashes it), classifies each by its verdict, harvests the ones newly terminal since the last
sweep, and prints a :class:`MonitorReport` as JSON for a harness cron to act on. The orchestration
lives on :class:`lote.cli.Lote`; this module holds the value objects it builds.
"""

from pydantic import computed_field

from .base import Field, FrozenModel


class Finished(FrozenModel):
    """A job that reached ``ok`` since the last sweep, with where its results were pulled.

    handle: the scheduler's job handle.
    target: the host alias it ran on.
    pulled_path: the local path its recorded results were rsynced into, or None when the run had no
        fetch path or the pull failed.
    """

    handle: str
    target: str
    pulled_path: str | None = None


class Failed(FrozenModel):
    """A job that ended badly (``failed`` or ``vanished``) since the last sweep, with the cause.

    handle: the scheduler's job handle.
    target: the host alias it ran on.
    reason: a short, network-free cause (a signal exit, a plain non-zero code, or that it is gone).
    """

    handle: str
    target: str
    reason: str


class DownHost(FrozenModel):
    """A host that could not be probed this sweep, so its jobs stay unresolved.

    host: the host alias.
    reason: why it could not be reached (``daemon down`` for a dead pueue, else ssh fault text).
    """

    host: str
    reason: str


class MonitorReport(FrozenModel):
    """One durable sweep's outcome, the JSON a harness cron reads from ``lote monitor --once``.

    ``changed`` is a computed field, so ``model_dump`` carries it: it is true exactly when this
    sweep harvested a job newly terminal since the last one, the cheap flag a cron branches on to
    skip a no-op tick.

    running: how many tracked jobs are still in flight.
    finished: jobs newly ``ok`` this sweep, each with its pulled results path.
    failed: jobs newly ``failed``/``vanished`` this sweep, each with a reason.
    unreachable_hosts: hosts that could not be probed, each with why.
    """

    running: int = 0
    finished: list[Finished] = Field(default_factory=list)
    failed: list[Failed] = Field(default_factory=list)
    unreachable_hosts: list[DownHost] = Field(default_factory=list)

    @computed_field
    @property
    def changed(self) -> bool:
        """Whether this sweep harvested any newly terminal job (the cron's skip-the-tick flag)."""
        return bool(self.finished or self.failed)
