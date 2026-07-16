"""The :class:`ReconcileRow` data structure plus the per-scheduler parse/verdict
helpers lote's reconcile uses.

A reconcile asks each backend's :meth:`Scheduler.state` what actually happened to
a recorded run; these helpers turn raw scheduler output (``qstat -f -H`` text, a
``pueue status`` task) into a state, exit code, and one-word verdict (``ok`` /
``failed`` / ``running`` / ``vanished`` / ``unknown``). The :class:`lote.cli.Lote`
builds one :class:`ReconcileRow` per run from the returned
:class:`lote.schedulers.JobState`.
"""

import pendulum

from .base import FrozenModel
from .clients import pueue


class ReconcileRow(FrozenModel):
    """One recorded run paired with its live scheduler state.

    handle: the run handle (PBS job id or pueue task id).
    script: the submitted script name.
    submitted_at: when the run was dispatched (from the cache).
    name: a human label for the run, shown instead of the internal script path when set.
    state: the scheduler's current state string, or None if the job vanished.
    exit_code: the job's exit status, when the scheduler reports one.
    verdict: ``ok`` / ``failed`` / ``running`` / ``vanished`` / ``unknown``.
    """

    handle: str
    script: str
    submitted_at: str
    name: str = ""
    state: str | None = None
    exit_code: int | None = None
    verdict: str


# PBS terminal states: the job has left the run queue.
PBS_FINISHED = {"F", "E"}
# pueue states that still mean "in flight".
PUEUE_LIVE = {pueue.PueueState.RUNNING, pueue.PueueState.QUEUED, pueue.PueueState.PAUSED}


def parse_pbs_record(record: str) -> tuple[str | None, int | None]:
    """Pull ``job_state`` and ``Exit_status`` out of a ``qstat -f`` block.

    Returns ``(None, None)`` when the job is absent from history (vanished).
    """
    if "Job Id:" not in record and "Job_Name" not in record and "job_state" not in record:
        return None, None
    state: str | None = None
    exit_code: int | None = None
    for line in record.splitlines():
        if " = " not in line:
            continue
        key, value = line.strip().split(" = ", maxsplit=1)
        if key == "job_state":
            state = value.strip()
        elif key == "Exit_status":
            exit_code = int(value.strip())
    return state, exit_code


def pbs_verdict(state: str | None, exit_code: int | None) -> str:
    """A one-word verdict for a PBS job from its state and exit status.

    A finished job with no ``Exit_status`` (e.g. qdel'd while still queued) is
    ``unknown``, never ``ok``, so ``lote run`` cannot report success for a job
    that produced nothing.
    """
    if state is None:
        return "vanished"
    if state not in PBS_FINISHED:
        return "running"
    if exit_code is None:
        return "unknown"
    return "ok" if exit_code == 0 else "failed"


def pueue_verdict(task: pueue.PueueTask | None) -> str:
    """A one-word verdict for a pueue task (None means it's gone from the queue)."""
    if task is None:
        return "vanished"
    if task.state in PUEUE_LIVE:
        return "running"
    return "ok" if task.succeeded else "failed"


def pueue_inherited(task: pueue.PueueTask, boundary: pendulum.DateTime) -> bool:
    """Whether a freshly (re)started daemon inherited ``task`` rather than launching it itself.

    True for an in-flight task (Running / Queued / Paused) that the current daemon did not start,
    meaning its run began before ``boundary`` or never began at all. When ``pueued`` restarts after
    a crash it resets every interrupted Running task to Queued (clearing its start) and pauses the
    group, so these inherited tasks are the zombies whose real process died with the old daemon,
    the phantoms the monitor would otherwise count running forever. A task the revived daemon
    genuinely relaunched carries a fresh start at or after ``boundary`` and is spared.

    boundary: when the daemon was revived; a run that began before it predates this daemon's life.
    """
    if task.state not in PUEUE_LIVE:
        return False
    return task.start is None or pendulum.parse(task.start) < boundary
