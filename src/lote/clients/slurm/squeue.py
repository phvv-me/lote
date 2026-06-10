import shlex

from plumbum import local

from ...log import logger
from ..machine import Machine
from ._common import parse_slurm_state
from .job_info import SlurmJob

# Pipe-delimited fields requested from ``squeue`` so parsing never relies on
# column widths: job id, name, state (long form), partition, elapsed time.
SQUEUE_FORMAT = "%i|%j|%T|%P|%M"


def build_squeue_command(*, me: bool = True, job_id: str | None = None) -> list[str]:
    """Build a ``squeue`` command emitting the pipe-delimited :data:`SQUEUE_FORMAT`.

    me: restrict to the current user's jobs (``--me``).
    job_id: restrict to a single job (``--job``).
    """
    command = ["squeue", "--noheader", f"--format={SQUEUE_FORMAT}"]
    if me:
        command.append("--me")
    if job_id is not None:
        command.extend(["--job", job_id])
    return command


def parse_squeue_output(output: str) -> list[SlurmJob]:
    """Parse the pipe-delimited ``squeue`` output into :class:`SlurmJob` rows."""
    jobs: list[SlurmJob] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        job_id, name, state, partition, elapsed = (part.strip() for part in parts[:5])
        jobs.append(
            SlurmJob(
                job_id=job_id,
                name=name,
                state=parse_slurm_state(state),
                partition=partition or None,
                elapsed=elapsed or None,
            )
        )
    return jobs


def squeue(
    *,
    me: bool = True,
    job_id: str | None = None,
    machine: Machine = local,
    parse_output: bool = True,
    dry_run: bool = False,
) -> list[SlurmJob] | str:
    """Run ``squeue`` on ``machine`` and optionally parse the output."""
    command = build_squeue_command(me=me, job_id=job_id)
    if dry_run:
        return shlex.join(command)
    logger.info("running {}", shlex.join(command))
    output = machine[command[0]][command[1:]]()
    return parse_squeue_output(output) if parse_output else output
