"""The PBS backend: the cluster (``qsub``) path, extracted verbatim from the
former ``machine.kind == "pbs"`` branches.

Every PBS verb runs through the host's ``jobs`` CLI in a login shell so the
cluster toolchain is on PATH. ``submit`` returns the job id; ``state`` reuses
the same ``qstat -f -H`` parse the standalone reconcile used.
"""

from typing import TYPE_CHECKING

from plumbum import FG

from ..clients.pbs import parse_qstat_output
from ..environment import Environment
from ..reconcile import parse_pbs_record, pbs_verdict
from .base import JobState, drain_log, poll_until_done, stream_until_done

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..clients.machine import Machine
    from .base import Resources


class Pbs:
    """Dispatch jobs to a PBS cluster via the on-host ``jobs qsub``."""

    name = "pbs"

    def submit(
        self, remote: Machine, root: str, script: str, args: Sequence[str], *, resources: Resources
    ) -> str:
        retcode, out, err = remote["bash"][
            ["-lc", Environment(root=root).exec_command("qsub", script, *args)]
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
        body = Environment(root=root).exec_command("info", handle)
        record = remote["bash"][["-lc", body]](retcode=None)
        state, exit_code = parse_pbs_record(record)
        return JobState(
            handle=handle, state=state, exit_code=exit_code, verdict=pbs_verdict(state, exit_code)
        )

    def wait(self, remote: Machine, root: str, handle: str) -> JobState:
        return poll_until_done(lambda: self.state(remote, root, handle))

    def stream(self, remote: Machine, root: str, handle: str) -> JobState:
        return stream_until_done(
            lambda: self.state(remote, root, handle),
            lambda offset: drain_log(remote, root, handle, offset),
        )

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        remote["bash"][["-lc", Environment(root=root).exec_command("cancel", handle)]] & FG
