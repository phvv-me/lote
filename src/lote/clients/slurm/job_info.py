from __future__ import annotations

from ...base import Model
from .job_state import SlurmState


class SlurmJob(Model):
    """One SLURM job row, parsed from ``squeue`` or ``sacct``.

    job_id: the SLURM job id (the lote handle for slurm targets).
    name: the job name (``--job-name``/``#SBATCH -J``).
    state: lifecycle state.
    exit_code: process exit code once terminal, parsed from ``sacct``'s
        ``ExitCode`` (``<code>:<signal>``); None while still live.
    partition: the partition the job runs on.
    elapsed: elapsed walltime string, when reported.
    """

    job_id: str
    name: str = ""
    state: SlurmState | str
    exit_code: int | None = None
    partition: str | None = None
    elapsed: str | None = None
