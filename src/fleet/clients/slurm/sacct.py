from __future__ import annotations

import shlex

from plumbum import local

from ...log import logger
from ..machine import Machine
from ._common import parse_exit_code, parse_slurm_state
from .job_info import SlurmJob

# The ``sacct`` columns fleet needs for a post-mortem, in this order.
SACCT_FORMAT = "JobID,State,ExitCode"


def build_sacct_command(job_id: str) -> list[str]:
    """Build the ``sacct`` post-mortem command for one job.

    Emits parseable, header-less, pipe-delimited rows of :data:`SACCT_FORMAT`.
    ``sacct`` reports the batch step (``<id>.batch``) and other sub-steps too;
    the parser keeps only the top-level ``<id>`` row.
    """
    return [
        "sacct",
        "--jobs",
        job_id,
        f"--format={SACCT_FORMAT}",
        "--parsable2",
        "--noheader",
    ]


def parse_sacct_output(output: str, job_id: str) -> SlurmJob | None:
    """Parse ``sacct`` output for ``job_id`` into a :class:`SlurmJob`.

    Returns None when the job is absent from the accounting database (vanished).
    Keeps the top-level ``<id>`` row, ignoring ``<id>.batch``/``.extern`` steps.
    """
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        row_id, state, exit_code = (part.strip() for part in parts[:3])
        if row_id != job_id:
            continue
        return SlurmJob(
            job_id=row_id,
            state=parse_slurm_state(state),
            exit_code=parse_exit_code(exit_code),
        )
    return None


def sacct(
    job_id: str,
    *,
    machine: Machine = local,
    parse_output: bool = True,
    dry_run: bool = False,
) -> SlurmJob | None | str:
    """Run ``sacct`` for ``job_id`` on ``machine`` and optionally parse it."""
    command = build_sacct_command(job_id)
    if dry_run:
        return shlex.join(command)
    logger.info("running {}", shlex.join(command))
    output = machine[command[0]][command[1:]]()
    return parse_sacct_output(output, job_id) if parse_output else output
