from __future__ import annotations

import shlex
from pathlib import Path

from plumbum import local

from ...log import logger
from ..machine import Machine
from ._common import extract_job_id


def build_sbatch_command(
    *,
    script: Path | str | None = None,
    gpus: int | None = None,
    walltime: str | None = None,
    partition: str | None = None,
    account: str | None = None,
    mem_gb: int | None = None,
    job_name: str | None = None,
    output_path: Path | str | None = None,
    export_vars: dict[str, str] | None = None,
) -> list[str]:
    """Build an ``sbatch`` command from explicit resource flags.

    gpus: ``--gpus=<n>`` request (omitted when None or 0).
    walltime: ``--time=<HH:MM:SS>``.
    partition: ``--partition=<name>``.
    account: ``--account=<name>``.
    mem_gb: ``--mem=<n>G``.
    output_path: merged stdout+stderr sink (``-o``); SLURM merges the two by
        default, mirroring PBS ``-j oe``.
    export_vars: ``--export=ALL,<K>=<V>,...`` so the job inherits the env plus
        these extras (matching the ``jobs run``/``qsub`` ``ARGS=`` convention).
    """
    command = ["sbatch"]
    if gpus:
        command.append(f"--gpus={gpus}")
    if walltime is not None:
        command.append(f"--time={walltime}")
    if partition is not None:
        command.append(f"--partition={partition}")
    if account is not None:
        command.append(f"--account={account}")
    if mem_gb is not None:
        command.append(f"--mem={mem_gb}G")
    if job_name is not None:
        command.append(f"--job-name={job_name}")
    if output_path is not None:
        command.append(f"--output={output_path}")
    if export_vars:
        pairs = ",".join(f"{key}={value}" for key, value in export_vars.items())
        command.append(f"--export=ALL,{pairs}")
    if script is not None:
        command.append(str(script))
    return command


def sbatch(
    *,
    script: Path | str | None = None,
    gpus: int | None = None,
    walltime: str | None = None,
    partition: str | None = None,
    account: str | None = None,
    mem_gb: int | None = None,
    job_name: str | None = None,
    output_path: Path | str | None = None,
    export_vars: dict[str, str] | None = None,
    machine: Machine = local,
    dry_run: bool = False,
) -> str:
    """Submit a SLURM batch job on ``machine`` and return its job id (or render it)."""
    command = build_sbatch_command(
        script=script,
        gpus=gpus,
        walltime=walltime,
        partition=partition,
        account=account,
        mem_gb=mem_gb,
        job_name=job_name,
        output_path=output_path,
        export_vars=export_vars,
    )
    if dry_run:
        return shlex.join(command)
    logger.info("running {}", shlex.join(command))
    return extract_job_id(machine[command[0]][command[1:]]())
