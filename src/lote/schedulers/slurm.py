"""The SLURM backend: jobs go to ``sbatch``, monitored with ``squeue``/``sacct``.

``submit`` builds the resource flags from :class:`Resources` and delegates to the
on-host ``jobs sbatch`` (login shell, so ``sbatch`` is on PATH); ``state`` runs
``sacct`` and parses ``State`` + ``ExitCode`` into a :class:`JobState`. There is
no live SLURM cluster in this repo, so every command is built by the
``lote.clients.slurm`` builders, keeping the backend unit-testable.
"""

import shlex
from typing import TYPE_CHECKING

from plumbum import FG

from ..clients.slurm import (
    SLURM_LIVE,
    SlurmState,
    build_sacct_command,
    build_sinfo_command,
    build_squeue_command,
    parse_sacct_output,
    parse_sinfo_output,
    parse_squeue_output,
)
from ..environment import Environment
from .base import JobState, drain_log, poll_until_done, stream_until_done

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
        overrides = build_sbatch_flags(resources)
        body = Environment(root=root).exec_command("sbatch", script, *overrides, *args)
        retcode, out, err = remote["bash"][["-lc", body]].run(retcode=None)
        # sbatch prints "Submitted batch job <id>"; take the trailing integer and
        # surface a failed submit instead of caching a blank/garbage handle (as Pbs
        # does). retcode=None keeps a non-zero remote sbatch from raising a raw
        # ProcessExecutionError before this friendly check runs.
        tokens = out.split()
        handle = tokens[-1] if tokens else ""
        if not handle.isdigit():
            raise SystemExit(
                f"sbatch failed (rc={retcode}): {(err or out).strip()[-400:] or '(no output)'}"
            )
        return handle

    def status(self, remote: Machine, root: str) -> None:
        remote["bash"][["-lc", Environment(root=root).exec_command("status")]] & FG

    def jobs(self, remote: Machine, root: str) -> list[JobState]:
        # `squeue` needs the cluster toolchain, so it runs under a login shell
        # (mirroring Pbs.jobs); the builder already scopes it to the current user.
        output = self.__cluster_command(remote, build_squeue_command(me=True))
        return [
            JobState(
                handle=job.job_id,
                label=job.name,
                state=str(job.state),
                verdict=slurm_verdict(job.state, None),
            )
            for job in parse_squeue_output(output)
        ]

    def logs(self, remote: Machine, root: str, handle: str) -> None:
        remote["bash"][["-lc", Environment(root=root).exec_command("logs", handle)]] & FG

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        output = self.__cluster_command(remote, build_sacct_command(handle))
        job = parse_sacct_output(output, handle)
        state = job.state if job else None
        exit_code = job.exit_code if job else None
        return JobState(
            handle=handle,
            state=str(state) if state is not None else None,
            exit_code=exit_code,
            verdict=slurm_verdict(state, exit_code),
        )

    def states(self, remote: Machine, root: str, handles: list[str]) -> dict[str, JobState]:
        # `squeue` lists the live jobs in one call; a finished job is no longer here, so status
        # falls back to a single `sacct` for those few (the fast path still covers the live ones).
        return {job.handle: job for job in self.jobs(remote, root)}

    def wait(self, remote: Machine, root: str, handle: str) -> JobState:
        return poll_until_done(lambda: self.state(remote, root, handle))

    def stream(self, remote: Machine, root: str, handle: str) -> JobState:
        return stream_until_done(
            lambda: self.state(remote, root, handle),
            lambda offset: drain_log(remote, root, handle, offset),
        )

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        remote["bash"][["-lc", Environment(root=root).exec_command("cancel", handle)]] & FG

    def revive(self, remote: Machine, root: str) -> list[str]:
        raise SystemExit("a SLURM scheduler is site-managed; there is no pueue daemon to revive")

    def queues(self, remote: Machine, root: str) -> list[str]:
        # `sinfo` enumerates the partitions (the cluster's node classes).
        return parse_sinfo_output(self.__cluster_command(remote, build_sinfo_command()))

    def __cluster_command(self, remote: Machine, command: list[str]) -> str:
        """Run a built ``squeue``/``sacct`` argv under ``bash -lc``, returning its stdout.

        A login shell sources ``/etc/profile.d``, putting the cluster toolchain on
        PATH -- the same treatment ``Pbs`` gives ``qstat``.
        """
        return str(remote["bash"][["-lc", shlex.join(command)]](retcode=None))


def build_sbatch_flags(resources: Resources) -> list[str]:
    """Render :class:`Resources` as ``jobs sbatch`` override flags.

    Only set fields become flags, so the script's own ``#SBATCH`` directives stay
    in effect for anything left unspecified -- including ``gpus``, omitted when 0
    so CPU-only jobs run on clusters without GPU GRES.
    """
    flags: list[str] = []
    if resources.gpus:
        flags.append(f"--gpus={resources.gpus}")
    if resources.walltime is not None:
        flags.append(f"--walltime={resources.walltime}")
    if resources.queue is not None:
        flags.append(f"--partition={resources.queue}")
    if resources.account is not None:
        flags.append(f"--account={resources.account}")
    if resources.mem_gb is not None:
        flags.append(f"--mem-gb={resources.mem_gb}")
    return flags
