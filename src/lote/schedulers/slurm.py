"""The SLURM backend: jobs go to ``sbatch``, monitored with ``squeue``/``sacct``.

``submit`` builds the resource flags from :class:`Resources` and delegates to the
on-host ``jobs sbatch`` (login shell, so ``sbatch`` is on PATH); ``state`` runs
``sacct`` and parses ``State`` + ``ExitCode`` into a :class:`JobState`. There is
no live SLURM cluster in this repo, so every command is built by a pure builder
(:func:`build_logs_command`) or the ``lote.clients.slurm`` builders, keeping the
backend unit-testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from plumbum import FG

from ..clients.slurm import SLURM_LIVE, SlurmState, build_sacct_command, parse_sacct_output
from ._remote import remote_exec
from .base import JobState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..clients.machine import Machine
    from .base import Resources


def slurm_verdict(state: SlurmState | str | None, exit_code: int | None) -> str:
    """A one-word verdict for a SLURM job from its ``sacct`` state and exit code.

    None state means the job is gone from the accounting database (vanished);
    a live state means still running; ``COMPLETED`` with code 0 is ``ok``;
    anything else terminal is ``failed``.
    """
    if state is None:
        return "vanished"
    if state in SLURM_LIVE:
        return "running"
    if state == SlurmState.COMPLETED and (exit_code or 0) == 0:
        return "ok"
    return "failed"


class Slurm:
    """Dispatch jobs to a SLURM cluster via the on-host ``jobs sbatch``."""

    name = "slurm"

    def submit(
        self, remote: Machine, root: str, script: str, args: Sequence[str], *, resources: Resources
    ) -> str:
        flags = build_sbatch_flags(resources)
        out = remote["bash"][["-lc", remote_exec(root, "sbatch", script, *flags, *args)]]()
        return out.strip().splitlines()[-1] if out.strip() else ""

    def status(self, remote: Machine, root: str) -> None:
        remote["bash"][["-lc", remote_exec(root, "status")]] & FG

    def logs(self, remote: Machine, root: str, handle: str, *, follow: bool) -> None:
        args = ["logs", handle, *(["--follow"] if follow else [])]
        remote["bash"][["-lc", remote_exec(root, *args)]] & FG

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        command = build_sacct_command(handle)
        output = remote[command[0]][command[1:]](retcode=None)
        job = parse_sacct_output(output, handle)
        state = job.state if job else None
        exit_code = job.exit_code if job else None
        return JobState(
            handle=handle,
            state=str(state) if state is not None else None,
            exit_code=exit_code,
            verdict=slurm_verdict(state, exit_code),
        )

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        remote["bash"][["-lc", remote_exec(root, "cancel", handle)]] & FG


def build_sbatch_flags(resources: Resources) -> list[str]:
    """Render :class:`Resources` as ``jobs sbatch`` override flags.

    Only set fields become flags, so the script's own ``#SBATCH`` directives
    stay in effect for anything left unspecified.
    """
    flags: list[str] = [f"--gpus={resources.gpus}"]
    if resources.walltime is not None:
        flags.append(f"--walltime={resources.walltime}")
    if resources.queue is not None:
        flags.append(f"--partition={resources.queue}")
    if resources.account is not None:
        flags.append(f"--account={resources.account}")
    if resources.mem_gb is not None:
        flags.append(f"--mem-gb={resources.mem_gb}")
    return flags
