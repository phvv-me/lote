"""The default ssh backend: jobs go to ``pueue`` (queue + exit codes + captured
logs). Extracted verbatim from the former non-PBS branches of the lote CLI.

``submit`` enqueues ``chefe run lote exec run <script> <args>`` with the
host's repo root as the working directory; ``state`` resolves a handle against a
single ``pueue status`` snapshot, reusing the same verdict logic reconcile used.
"""

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from plumbum import FG

from .. import NAME
from ..clients import pueue
from ..environment import Environment
from ..reconcile import pueue_verdict
from ..render import Renderer
from .base import JobState, poll_until_done

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

    def jobs(self, remote: Machine, root: str) -> list[JobState]:
        return [
            JobState(
                handle=str(task.id),
                label=task.label,
                state=str(task.state),
                exit_code=task.exit_code,
                verdict=pueue_verdict(task),
            )
            for task in pueue.status(machine=remote, root=root)
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

    def wait(self, remote: Machine, root: str, handle: str) -> JobState:
        return poll_until_done(lambda: self.state(remote, root, handle))

    def stream(self, remote: Machine, root: str, handle: str) -> JobState:
        # pueue's native follow already prints live output and exits when the task
        # ends, so streaming is one follow plus a state read for the final verdict.
        pueue.binary(remote, root)[["follow", handle]] & FG
        return self.wait(remote, root, handle)

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        pueue.kill(handle, machine=remote, root=root)
