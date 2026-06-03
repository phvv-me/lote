"""The :class:`Scheduler` contract lote dispatches through.

Every job backend (pueue, PBS, SLURM, plain bash) implements this protocol so
``lote submit``/``status``/``logs``/``info``/``reconcile`` read as
orchestration: pick the target's scheduler, then delegate. New backends are new
classes, never new ``if machine.kind == ...`` branches.

A backend operates on two things: a plumbum ``remote`` machine (an open
``SshMachine`` to the host) and the repo ``root`` on that host. Methods that
need the cluster toolchain on PATH run through the host's ``jobs`` CLI in a
login shell, exactly as the standalone lote did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..base import FrozenModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..clients.machine import Machine
    from ..models import Target


class Resources(FrozenModel):
    """A scheduler-agnostic resource request for one job.

    Each backend maps these onto its own flags (``-l select=`` for PBS,
    ``--gpus``/``--mem`` for SLURM) and ignores what it can't express.

    gpus: number of GPUs/nodes to request.
    walltime: requested walltime as ``HH:MM:SS``, when capped.
    queue: scheduler queue/partition name.
    account: charging account / group list.
    mem_gb: system memory request in GB.
    """

    gpus: int = 1
    walltime: str | None = None
    queue: str | None = None
    account: str | None = None
    mem_gb: int | None = None


class JobState(FrozenModel):
    """A job's post-mortem state, the unit ``reconcile`` compares against the cache.

    handle: the scheduler's job handle (PBS job id, pueue task id, SLURM job id).
    state: the scheduler's current state string, or None when the job vanished.
    exit_code: the process exit status, when the scheduler reports one.
    verdict: one word — ``ok`` / ``failed`` / ``running`` / ``vanished``.
    """

    handle: str
    state: str | None = None
    exit_code: int | None = None
    verdict: str


@runtime_checkable
class Scheduler(Protocol):
    """A pluggable job backend lote dispatches generically.

    ``remote`` is an open plumbum ``SshMachine`` (or ``local``); ``root`` is the
    repo path on the host. Implementations are stateless value objects, so one
    instance per kind is enough.
    """

    name: str

    def submit(
        self, remote: Machine, root: str, script: str, args: Sequence[str], *, resources: Resources
    ) -> str:
        """Launch ``script`` with ``args`` under ``resources``; return the job handle."""
        ...

    def status(self, remote: Machine, root: str) -> None:
        """Render the user's live jobs on the host (may delegate to ``jobs status``)."""
        ...

    def logs(self, remote: Machine, root: str, handle: str, *, follow: bool) -> None:
        """Tail ``handle``'s captured log (merged stdout+stderr)."""
        ...

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        """Post-mortem ``handle``: its state, exit code, and a verdict, for reconcile."""
        ...

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        """Cancel ``handle`` on the host."""
        ...


def pick(target: Target) -> Scheduler:
    """Return the :class:`Scheduler` for ``target`` from the probed ``kind``.

    ``pbs`` -> :class:`Pbs`, ``slurm`` -> :class:`Slurm`, ``ssh`` -> :class:`Pueue`
    (the default ssh queue). The map lives here so adding a backend is one entry.
    """
    from .pbs import Pbs
    from .pueue import Pueue
    from .slurm import Slurm

    schedulers: dict[str, Scheduler] = {"pbs": Pbs(), "slurm": Slurm(), "ssh": Pueue()}
    return schedulers.get(target.kind, Pueue())
