"""The :class:`ReconcileRow` data structure plus the per-scheduler parse/verdict
helpers lote's reconcile uses.

A reconcile asks each backend's :meth:`Scheduler.state` what actually happened to
a recorded run; these helpers turn raw scheduler output (``qstat -f -H`` text, a
``pueue status`` task) into a state, exit code, and one-word verdict (``ok`` /
``failed`` / ``running`` / ``vanished``). The :class:`lote.cli.Lote` builds one
:class:`ReconcileRow` per run from the returned :class:`lote.schedulers.JobState`.
"""

from __future__ import annotations

from .base import FrozenModel
from .clients import pueue


class ReconcileRow(FrozenModel):
    """One recorded run paired with its live scheduler state.

    handle: the run handle (PBS job id or pueue task id).
    script: the submitted script name.
    submitted_at: when the run was dispatched (from the cache).
    state: the scheduler's current state string, or None if the job vanished.
    exit_code: the job's exit status, when the scheduler reports one.
    verdict: ``ok`` / ``failed`` / ``running`` / ``vanished``.
    """

    handle: str
    script: str
    submitted_at: str
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
    """A one-word verdict for a PBS job from its state and exit status."""
    if state is None:
        return "vanished"
    if state not in PBS_FINISHED:
        return "running"
    if exit_code is None:
        return "ok"
    return "ok" if exit_code == 0 else "failed"


def pueue_verdict(task: pueue.PueueTask | None) -> str:
    """A one-word verdict for a pueue task (None means it's gone from the queue)."""
    if task is None:
        return "vanished"
    if task.state in PUEUE_LIVE:
        return "running"
    return "ok" if task.succeeded else "failed"
