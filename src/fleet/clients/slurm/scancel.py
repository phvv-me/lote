from __future__ import annotations

import shlex
from collections.abc import Sequence

from plumbum import local

from ...log import logger
from ..machine import Machine


def build_scancel_command(job_ids: str | Sequence[str]) -> list[str]:
    """Build a ``scancel`` command for one or more job ids."""
    ids = [job_ids] if isinstance(job_ids, str) else list(job_ids)
    return ["scancel", *ids]


def scancel(
    job_ids: str | Sequence[str],
    *,
    machine: Machine = local,
    dry_run: bool = False,
) -> str:
    """Cancel one or more SLURM jobs on ``machine`` (or render the command)."""
    command = build_scancel_command(job_ids)
    if dry_run:
        return shlex.join(command)
    logger.info("running {}", shlex.join(command))
    return str(machine[command[0]][command[1:]]())
