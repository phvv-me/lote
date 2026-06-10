"""The no-scheduler backend: run the job straight through the host's ``jobs run``
(plain ``bash``), for ssh hosts without a ``pueue`` daemon.

There is no queue and no persistent handle, so ``submit`` blocks until the job
finishes and ``state`` can only report a vanished post-mortem; ``status`` is a
no-op. Use :class:`Pueue` instead whenever a daemon is available — this is the
bare fallback.
"""

from typing import TYPE_CHECKING

from plumbum import FG

from ..environment import Environment
from ..log import logger
from .base import JobState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..clients.machine import Machine
    from .base import Resources


class Local:
    """Run jobs directly through ``bash`` on the host (no scheduler, no queue)."""

    name = "local"

    def submit(
        self, remote: Machine, root: str, script: str, args: Sequence[str], *, resources: Resources
    ) -> str:
        remote["bash"][["-lc", Environment(root=root).exec_command("run", script, *args)]] & FG
        return script

    def status(self, remote: Machine, root: str) -> None:
        logger.info("local backend has no queue; nothing to show")

    def jobs(self, remote: Machine, root: str) -> list[JobState]:
        return []

    def logs(self, remote: Machine, root: str, handle: str) -> None:
        remote["bash"][["-lc", Environment(root=root).exec_command("logs", handle)]] & FG

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        return JobState(handle=handle, state=None, exit_code=None, verdict="vanished")

    def wait(self, remote: Machine, root: str, handle: str) -> JobState:
        # `submit` ran the job to completion in the foreground and raised on a non-zero
        # exit, so a handle that reaches here finished fine; there is nothing to poll.
        return JobState(handle=handle, state="done", exit_code=0, verdict="ok")

    def stream(self, remote: Machine, root: str, handle: str) -> JobState:
        # `submit` already relayed the job's output in the foreground; nothing to follow.
        return self.wait(remote, root, handle)

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        logger.info("local backend has no queue; cannot cancel {}", handle)
