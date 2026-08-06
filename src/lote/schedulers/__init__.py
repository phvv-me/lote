from typing import TYPE_CHECKING

from patos import Strategy

from ..transport import DaemonDown, HostUnreachable
from .base import (
    JobState,
    Resources,
    Scheduler,
    exit_reason,
    failure_reason,
    log_excerpt,
    login_run,
    poll_until_done,
    read_log,
    short_reason,
    stream_until_done,
    verdict_line,
)
from .local import Local
from .pbs import Pbs, build_qsub_flags
from .pueue import Pueue
from .slurm import Slurm, build_sbatch_flags, slurm_verdict

if TYPE_CHECKING:
    from ..models import Target

# The backend registry, built once at import: a probed (or hinted) ``kind``
# selects its scheduler. Adding a backend is one `register` line here.
SCHEDULERS: Strategy[Scheduler] = Strategy("scheduler")
SCHEDULERS.register("pbs", Pbs())
SCHEDULERS.register("slurm", Slurm())
SCHEDULERS.register("ssh", Pueue())
SCHEDULERS.register("local", Local())


def pick(target: Target) -> Scheduler:
    """Return the :class:`Scheduler` for ``target`` from the probed ``kind``.

    ``pbs`` -> :class:`Pbs`, ``slurm`` -> :class:`Slurm`, ``ssh`` -> :class:`Pueue`
    (the default ssh queue), ``local`` -> :class:`Local` (bare bash, no daemon).
    An unknown kind raises a clear lookup error instead of silently dispatching
    somewhere a typo'd ``[hints]`` entry never meant to go.
    """
    return SCHEDULERS.select(target.kind)


__all__ = [
    "SCHEDULERS",
    "DaemonDown",
    "HostUnreachable",
    "JobState",
    "Local",
    "Pbs",
    "Pueue",
    "Resources",
    "Scheduler",
    "Slurm",
    "build_sbatch_flags",
    "build_qsub_flags",
    "exit_reason",
    "failure_reason",
    "log_excerpt",
    "login_run",
    "pick",
    "poll_until_done",
    "read_log",
    "short_reason",
    "slurm_verdict",
    "stream_until_done",
    "verdict_line",
]
