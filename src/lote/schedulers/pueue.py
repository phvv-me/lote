"""The default ssh backend: jobs go to ``pueue`` (queue + exit codes + captured
logs). Extracted verbatim from the former non-PBS branches of the lote CLI.

``submit`` enqueues ``chefe run lote exec run <script> <args>`` with the
host's repo root as the working directory; ``state`` resolves a handle against a
single ``pueue status`` snapshot, reusing the same verdict logic reconcile used.
"""

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import pendulum
from plumbum import FG
from plumbum.commands.processes import ProcessExecutionError

from .. import NAME
from ..clients import pueue
from ..environment import Environment
from ..reconcile import pueue_inherited, pueue_verdict
from ..render import Renderer
from .base import JobState, poll_until_done

# pueue states with a live child process, the ones a zombie must be killed out of before it can be
# removed (a Queued/Stashed/Done task is removed directly).
_PUEUE_KILLABLE = {pueue.PueueState.RUNNING, pueue.PueueState.PAUSED}

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..clients.machine import Machine
    from .base import Resources


class Pueue:
    """Dispatch jobs to a host's ``pueue`` daemon (the ssh default)."""

    name = "ssh"

    def __init__(self) -> None:
        self.__render = Renderer()

    def submit(
        self, remote: Machine, root: str, script: str, args: Sequence[str], *, resources: Resources
    ) -> str:
        # pueue runs each task in a bare subshell that inherits the daemon's
        # environment, which need not carry the user install dirs; Environment
        # prepends them (so `chefe` resolves) and pueue owns the cwd (cd off).
        arg_str = " ".join(shlex.quote(arg) for arg in args)
        command = f"{NAME} exec run {shlex.quote(script)} {arg_str}".rstrip()
        inner = Environment(root=root).wrap(command, cd=False)
        return pueue.add(
            inner, machine=remote, root=root, label=Path(script).stem, working_directory=root
        )

    def status(self, remote: Machine, root: str) -> None:
        self.__render.tasks(pueue.status(machine=remote, root=root))

    @staticmethod
    def _job_state(task: pueue.PueueTask) -> JobState:
        return JobState(
            handle=str(task.id),
            label=task.label,
            state=str(task.state),
            exit_code=task.exit_code,
            verdict=pueue_verdict(task),
        )

    def jobs(self, remote: Machine, root: str) -> list[JobState]:
        return [
            self._job_state(task)
            for task in pueue.status(machine=remote, root=root)
            if task.state is not pueue.PueueState.DONE
        ]

    def logs(self, remote: Machine, root: str, handle: str) -> None:
        print(pueue.log(handle, machine=remote, root=root))

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        task = next(
            (task for task in pueue.status(machine=remote, root=root) if str(task.id) == handle),
            None,
        )
        return JobState(
            handle=handle,
            state=str(task.state) if task else None,
            exit_code=task.exit_code if task else None,
            verdict=pueue_verdict(task),
        )

    def states(self, remote: Machine, root: str, handles: list[str]) -> dict[str, JobState]:
        # One `pueue status` carries running and finished tasks with exit codes, so the whole host
        # resolves in one call regardless of `handles`. Finished tasks remain visible here because
        # reconciliation still needs their terminal verdict after `jobs()` stops listing them.
        return {
            str(task.id): self._job_state(task) for task in pueue.status(machine=remote, root=root)
        }

    def wait(self, remote: Machine, root: str, handle: str) -> JobState:
        return poll_until_done(lambda: self.state(remote, root, handle))

    def stream(self, remote: Machine, root: str, handle: str) -> JobState:
        # pueue's native follow already prints live output and exits when the task
        # ends, so streaming is one follow plus a state read for the final verdict.
        try:
            pueue.binary(remote, root)[["follow", handle]] & FG
        except ProcessExecutionError:
            final = self.state(remote, root, handle)
            if final.verdict == "vanished":
                return final
            raise
        return self.wait(remote, root, handle)

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        pueue.cancel(handle, machine=remote, root=root)

    def revive(self, remote: Machine, root: str) -> list[str]:
        """Restart the daemon, retire the zombie tasks it inherits, then resume the queue.

        The one backend with a user-managed daemon: a dead ``pueued`` is restarted with ``pueued
        -d`` (idempotent, so a host whose daemon is already up is left as is). pueue's own crash
        recovery then resets every task that was running when the daemon died to Queued and pauses
        the group, so those tasks read as still in flight while their real process is gone. Any
        in-flight task that predates this revive (:func:`pueue_inherited`) is therefore a zombie
        and never a job the just-restarted daemon launched, so it is killed if needed, removed (so
        it resolves to ``vanished``), and the group is resumed so the revived host runs new work.
        Returns the cleared handles for the caller to report.
        """
        before = pendulum.now()
        pueue.shutdown(machine=remote, root=root)
        pueue.start(machine=remote, root=root)
        zombies = [
            task
            for task in pueue.status(machine=remote, root=root)
            if pueue_inherited(task, before)
        ]
        live = [str(task.id) for task in zombies if task.state in _PUEUE_KILLABLE]
        if live:
            pueue.kill(live, machine=remote, root=root)
        handles = [str(task.id) for task in zombies]
        if handles:
            pueue.remove(handles, machine=remote, root=root)
        pueue.resume(machine=remote, root=root)
        return handles

    def queues(self, remote: Machine, root: str) -> list[str]:
        # pueue is one queue on one machine; the login class already describes it.
        return []
