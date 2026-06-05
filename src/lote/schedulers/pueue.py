"""The default ssh backend: jobs go to ``pueue`` (queue + exit codes + captured
logs). Extracted verbatim from the former non-PBS branches of the lote CLI.

``submit`` enqueues ``chefe run lote exec run <script> <args>`` with the
host's repo root as the working directory; ``state`` resolves a handle against a
single ``pueue status`` snapshot, reusing the same verdict logic reconcile used.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from plumbum import FG

from .. import NAME
from ..clients import pueue
from ..reconcile import pueue_verdict
from ..render import Renderer
from .base import JobState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..clients.machine import Machine
    from .base import Resources


# pueue runs each task in a bare subshell that inherits the daemon's environment,
# which need not carry the per-user install dirs on PATH. The dispatched command
# puts them on first, so `chefe` (and the pixi/cargo engines it manages) resolve
# however pueued was started — the same dirs setup.sh exports.
USER_BINS = "$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin"


class Pueue:
    """Dispatch jobs to a host's ``pueue`` daemon (the ssh default)."""

    name = "ssh"

    def __init__(self) -> None:
        self.__render = Renderer()

    def submit(
        self, remote: Machine, root: str, script: str, args: Sequence[str], *, resources: Resources
    ) -> str:
        arg_str = " ".join(shlex.quote(arg) for arg in args)
        command = f"chefe run {NAME} exec run {shlex.quote(script)} {arg_str}".rstrip()
        inner = f"export PATH={USER_BINS}:$PATH; {command}"
        return pueue.add(inner, machine=remote, label=Path(script).stem, working_directory=root)

    def status(self, remote: Machine, root: str) -> None:
        self.__render.tasks(pueue.status(machine=remote))

    def logs(self, remote: Machine, root: str, handle: str, *, follow: bool) -> None:
        if follow:
            remote["pueue"][["follow", handle]] & FG
            return
        print(pueue.log(handle, machine=remote))

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        task = next(
            (task for task in pueue.status(machine=remote) if str(task.id) == handle), None
        )
        return JobState(
            handle=handle,
            state=str(task.state) if task else None,
            exit_code=task.exit_code if task else None,
            verdict=pueue_verdict(task),
        )

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        pueue.kill(handle, machine=remote)
