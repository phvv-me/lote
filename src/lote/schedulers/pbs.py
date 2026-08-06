"""The PBS backend: the cluster (``qsub``) path, extracted verbatim from the
former ``machine.kind == "pbs"`` branches.

Every PBS verb runs through the host's ``jobs`` CLI in a login shell so the
cluster toolchain is on PATH. ``submit`` returns the job id. ``states``/``state``
resolve handles with two batched ``qstat -f`` queries (live, then ``-H`` history
for the leftovers), because a live job is absent from ``-H`` on some deployments
(Miyabi's wrapper) and a purged job is absent from both; a handle neither query
knows is settled by :meth:`Pbs.autopsy` from the exit artifact the generated job
script writes on the host, so a walltime-killed job whose history entry is gone
still reconciles to ``ok``/``failed`` instead of reading ``running`` forever.
"""

import re
import shlex
from typing import TYPE_CHECKING

from plumbum import FG

from ..clients.pbs import (
    parse_qstat_full,
    parse_qstat_output,
    parse_qstat_queues,
    parse_rsc_queues,
)
from ..environment import Environment
from ..reconcile import pbs_verdict
from .base import JobState, drain_log, login_run, poll_until_done, stream_until_done

# The exit artifact the generated PBS job script traps out on the host:
# `.lote/logs/<bare jobid>.exit` holding one `exit=N` line.
_EXIT_ARTIFACT = re.compile(r"exit=(\d+)")


def bare(handle: str) -> str:
    """A PBS handle's bare job number: ``2435326.opbs`` and ``2435326`` both -> ``2435326``.

    The cache records what ``qsub`` printed (bare on Miyabi's wrapper, ``<id>.<server>``
    elsewhere) while ``qstat -f`` always reports the full id, so every lookup joins on the
    bare number rather than trusting the two spellings to agree.
    """
    return handle.split(".", maxsplit=1)[0]


def build_qsub_flags(resources: Resources) -> list[str]:
    """Render set scheduler resources as on-host executor overrides."""
    flags: list[str] = []
    if resources.queue is not None:
        flags.append(f"--queue={resources.queue}")
    if resources.walltime is not None:
        flags.append(f"--walltime={resources.walltime}")
    if resources.account is not None:
        flags.append(f"--group-list={resources.account}")
    if resources.mem_gb is not None:
        flags.append(f"--mem-gb={resources.mem_gb}")
    return flags


if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..clients.machine import Machine
    from ..clients.pbs import JobInfo
    from .base import Resources


class Pbs:
    """Dispatch jobs to a PBS cluster via the on-host ``jobs qsub``."""

    name = "pbs"

    def submit(
        self, remote: Machine, root: str, script: str, args: Sequence[str], *, resources: Resources
    ) -> str:
        overrides = build_qsub_flags(resources)
        retcode, out, err = remote["bash"][
            ["-lc", Environment(root=root).exec_command("qsub", script, *overrides, *args)]
        ].run(retcode=None)
        handle = out.strip().splitlines()[-1] if out.strip() else ""
        # a valid PBS handle starts with the job number; anything else (a parent
        # queue, a quota reject) means qsub failed -- surface it, never a blank id.
        if not handle[:1].isdigit():
            raise SystemExit(f"qsub failed (rc={retcode}): {(err or out).strip()[-400:]}")
        return handle

    def status(self, remote: Machine, root: str) -> None:
        remote["bash"][["-lc", Environment(root=root).exec_command("status")]] & FG

    def jobs(self, remote: Machine, root: str) -> list[JobState]:
        # `qstat` needs the cluster toolchain, so it runs under a login shell; the bare
        # form already lists only the current user's jobs, which is the live `ps` view.
        output = remote["bash"][["-lc", "qstat"]](retcode=None)
        return [
            JobState(
                handle=job.job_id,
                label=job.name or None,
                state=str(job.state),
                verdict=pbs_verdict(str(job.state), None),
            )
            for job in parse_qstat_output(output)
        ]

    def logs(self, remote: Machine, root: str, handle: str) -> None:
        remote["bash"][["-lc", Environment(root=root).exec_command("logs", handle)]] & FG

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        # login_run raises HostUnreachable on an ssh transport failure, so a refused control-master
        # session is retried by the wait loop rather than parsed as an empty (vanished) record.
        found = self.states(remote, root, [handle]).get(handle)
        return found if found is not None else self.autopsy(remote, root, handle)

    def states(self, remote: Machine, root: str, handles: list[str]) -> dict[str, JobState]:
        # Two batched round-trips resolve a whole host without an ssh per handle: `qstat -f` full
        # records for the live jobs, then `qstat -f -H` history for whatever the live query missed.
        # They must stay separate queries because on some deployments (Miyabi's wrapper) a live job
        # never appears under `-H` and a finished one never appears without it. Ids purged from
        # both are simply absent; the caller settles those through `state` -> `autopsy`.
        if not handles:
            return {}
        found = self.__query(remote, "qstat -f", handles)
        if missing := [h for h in handles if h not in found]:
            found |= self.__query(remote, "qstat -f -H", missing)
        return found

    def __query(self, remote: Machine, command: str, handles: list[str]) -> dict[str, JobState]:
        """One batched full-record qstat, keyed back to the requested handles by bare job number.

        qstat reports full ``<id>.<server>`` ids while the cache may hold the bare number the
        wrapper's qsub printed, so records join on :func:`bare`; a handle with no record is
        simply absent from the result.
        """
        output = login_run(remote, f"{command} " + " ".join(shlex.quote(h) for h in handles))
        records = {bare(job.job_id): job for job in parse_qstat_full(output)}
        return {
            handle: self.__job_state(handle, record)
            for handle in handles
            if (record := records.get(bare(handle))) is not None
        }

    @staticmethod
    def __job_state(handle: str, job: JobInfo) -> JobState:
        return JobState(
            handle=handle,
            label=job.name or None,
            state=str(job.state),
            exit_code=job.exit_status,
            verdict=pbs_verdict(str(job.state), job.exit_status),
        )

    def autopsy(self, remote: Machine, root: str, handle: str) -> JobState:
        """Settle a handle the scheduler no longer remembers from its on-host exit artifact.

        The generated PBS job script traps its exit into ``.lote/logs/<bare jobid>.exit``, so a
        job that finished after the server purged its history (or that PBS never recorded, like
        the walltime kills Miyabi drops from ``qstat``) still reconciles to a real ``ok``/
        ``failed`` with its exit code. No artifact (a hand-written script, a SIGKILL that ran no
        trap) means the job is genuinely ``vanished``.
        """
        artifact = shlex.quote(f"{root}/.lote/logs/{bare(handle)}.exit")
        out = login_run(remote, f"cat {artifact} 2>/dev/null")
        if match := _EXIT_ARTIFACT.search(out):
            code = int(match.group(1))
            return JobState(
                handle=handle,
                state="artifact",
                exit_code=code,
                verdict="ok" if code == 0 else "failed",
            )
        return JobState(handle=handle, state=None, exit_code=None, verdict="vanished")

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
        raise SystemExit("a PBS scheduler is site-managed; there is no pueue daemon to revive")

    def queues(self, remote: Machine, root: str) -> list[str]:
        # `qstat -q` enumerates every queue (the host's node classes); it needs the
        # cluster toolchain, so it runs under a login shell like the other PBS verbs.
        output = remote["bash"][["-lc", "qstat -q"]](retcode=None)
        if standard := parse_qstat_queues(output):
            return standard
        # Miyabi's qstat wrapper rejects -q; its --rsc tree lists the queues
        return parse_rsc_queues(remote["bash"][["-lc", "qstat --rsc"]](retcode=None))
