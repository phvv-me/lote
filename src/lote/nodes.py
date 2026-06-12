"""Per-queue node-class discovery: one minimal scheduler job per queue.

The login node's class comes from the stock over-ssh probe, but a cluster's
real hardware sits behind its queues. So onboarding asks the scheduler for its
queue list (PBS ``qstat -q``, SLURM ``sinfo``) and submits one tiny job to
each, whose whole payload is printing mainboard's machine snapshot. The
snapshot parsed from the job's captured log becomes that queue's
:class:`NodeClass`, cached under the class key, and special classes like
Miyabi's ``prepost`` movers are found rather than configured.
"""

import json
from time import monotonic, sleep
from typing import TYPE_CHECKING

from .jobspec import JobSpec
from .models import NodeClass, Snapshot
from .schedulers import JobState
from .schedulers.base import POLL_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

    from .clients.machine import Machine
    from .schedulers import Scheduler

# The probe job's whole payload: print the node's mainboard snapshot as one JSON line.
PROBE_COMMAND = 'python -c "from mainboard import Machine; print(Machine().model_dump_json())"'

# Short enough for any queue's walltime cap, long enough for a cold import.
PROBE_WALLTIME = "00:10:00"

# How long discovery waits for one queue's probe job before skipping that class.
PROBE_WAIT = 600.0


def probe_spec(queue: str) -> JobSpec:
    """The minimal job submitted to ``queue``: one node, no GPUs, the mainboard dump.

    queue: the scheduler queue / partition to probe.
    """
    return JobSpec(cmd=PROBE_COMMAND, queue=queue, walltime=PROBE_WALLTIME)


def parse_snapshot(queue: str, log: str) -> NodeClass:
    """The probed :class:`NodeClass` from a probe job's captured log.

    The snapshot is the last JSON-object line (activation noise and module
    chatter may precede it); a log without one raises the ``LookupError`` the
    caller turns into a skipped class.

    queue: the class key the capabilities go under.
    log: the probe job's captured stdout+stderr.
    """
    for line in reversed(log.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            return Snapshot.model_validate(json.loads(candidate)).node_class(queue)
        except ValueError:
            continue
    raise LookupError(f"no machine snapshot in the probe log for queue {queue!r}")


def wait_for(
    scheduler: Scheduler,
    remote: Machine,
    root: str,
    handle: str,
    *,
    timeout: float = PROBE_WAIT,
    interval: float = POLL_SECONDS,
    sleeper: Callable[[float], None] = sleep,
    clock: Callable[[], float] = monotonic,
) -> JobState:
    """Poll ``handle`` until it is terminal or ``timeout`` passes, whichever first.

    A probe job stuck in a busy queue must not hang discovery, so hitting the
    deadline cancels the job and reports a ``timeout`` verdict the caller skips
    on. ``sleeper``/``clock`` are injected so a test drives the loop without
    real time passing.

    timeout: seconds to wait before cancelling the job.
    interval: seconds between polls.
    """
    deadline = clock() + timeout
    while (state := scheduler.state(remote, root, handle)).verdict == "running":
        if clock() >= deadline:
            scheduler.cancel(remote, root, handle)
            return JobState(handle=handle, state=state.state, verdict="timeout")
        sleeper(interval)
    return state
