"""The PBS backend: the cluster (``qsub``) path, extracted verbatim from the
former ``machine.kind == "pbs"`` branches.

Every PBS verb runs through the host's ``jobs`` CLI in a login shell so the
cluster toolchain is on PATH. ``submit`` returns the job id; ``state`` reuses
the same ``qstat -f -H`` parse the standalone reconcile used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from plumbum import FG

from ..reconcile import parse_pbs_record, pbs_verdict
from ._remote import remote_exec
from .base import JobState

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
        out = remote["bash"][["-lc", remote_exec(root, "qsub", script, *args)]]()
        return out.strip().splitlines()[-1] if out.strip() else ""

    def status(self, remote: Machine, root: str) -> None:
        remote["bash"][["-lc", remote_exec(root, "status")]] & FG

    def logs(self, remote: Machine, root: str, handle: str, *, follow: bool) -> None:
        args = ["logs", handle, *(["--follow"] if follow else [])]
        remote["bash"][["-lc", remote_exec(root, *args)]] & FG

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        record = remote["bash"][["-lc", remote_exec(root, "info", handle)]](retcode=None)
        state, exit_code = parse_pbs_record(record)
        return JobState(
            handle=handle, state=state, exit_code=exit_code, verdict=pbs_verdict(state, exit_code)
        )

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        remote["bash"][["-lc", remote_exec(root, "cancel", handle)]] & FG
