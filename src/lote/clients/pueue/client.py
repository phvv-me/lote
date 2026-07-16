import json
import shlex
from collections.abc import Sequence
from pathlib import Path

from plumbum import local
from plumbum.commands.base import BaseCommand
from plumbum.commands.processes import ProcessExecutionError

from ...transport import DaemonDown, daemon_failure
from ..machine import Machine
from .state import PueueState
from .task import PueueTask

# pueue ships in the compiled chefe env (chefe.toml [deps]), not in a user bin dir, so a
# bare `pueue` is absent from a fresh host's PATH. The env copy under the repo root wins;
# PATH stays as the fallback for hosts that still carry a cargo-installed pueue.
ENV_PUEUE = ".chefe/.pixi/envs/default/bin/pueue"
ENV_PUEUED = ".chefe/.pixi/envs/default/bin/pueued"
_KILLABLE = {PueueState.RUNNING, PueueState.PAUSED}
_REMOVABLE = {PueueState.LOCKED, PueueState.STASHED, PueueState.QUEUED}


def binary(machine: Machine, root: Path | str | None = None) -> BaseCommand:
    """The `pueue` command on ``machine``: the chefe env copy under ``root``, else PATH's."""
    if root is not None:
        env_pueue = machine.path(str(root)) / ENV_PUEUE
        if env_pueue.exists():
            return machine[str(env_pueue)]
    return machine["pueue"]


def start(*, machine: Machine = local, root: Path | str | None = None) -> str:
    """Start the pueue daemon detached (``pueued -d``), reviving a host whose queue died.

    Mirrors :func:`binary`: the chefe env copy of ``pueued`` under ``root`` wins, with PATH's as
    the fallback for a host carrying a cargo-installed daemon. ``-d`` daemonizes, so the call
    returns at once and the daemon outlives the ssh session that launched it.
    """
    executable = "pueued"
    if root is not None:
        env_pueued = machine.path(str(root)) / ENV_PUEUED
        if env_pueued.exists():
            executable = str(env_pueued)
    command = f"{shlex.quote(executable)} -d >/dev/null 2>&1"
    return str(machine["sh"][["-c", command]]())


def shutdown(*, machine: Machine = local, root: Path | str | None = None) -> str:
    """Stop the pueue daemon, or do nothing when it is already down."""
    try:
        return str(binary(machine, root)[["shutdown"]]())
    except ProcessExecutionError as error:
        if daemon_failure(error.stderr or ""):
            return ""
        raise


def add(
    command: str,
    *,
    machine: Machine = local,
    root: Path | str | None = None,
    label: str | None = None,
    group: str | None = None,
    after: Sequence[int | str] = (),
    immediate: bool = False,
    working_directory: Path | str | None = None,
) -> str:
    """Enqueue ``command`` on ``machine`` and return its task id.

    pueue runs the trailing string in a subshell, so pass the whole command as one
    string to keep its quoting intact. ``machine`` is plumbum's ``local`` or an
    ``SshMachine`` — the same call queues locally or on a remote host.
    """
    args = ["add", "--print-task-id"]
    if label is not None:
        args += ["--label", label]
    if group is not None:
        args += ["--group", group]
    for dependency in after:
        args += ["--after", str(dependency)]
    if immediate:
        args.append("--immediate")
    if working_directory is not None:
        args += ["--working-directory", str(working_directory)]
    return str(binary(machine, root)[[*args, "--", command]]().strip())


def status(
    *, machine: Machine = local, root: Path | str | None = None, group: str | None = None
) -> list[PueueTask]:
    """Return the queue's tasks, parsed from ``pueue status --json``.

    A task's ``status`` is externally tagged — ``{"Running": {...}}`` or
    ``{"Done": {"start", "end", "result", ...}}`` — and ``result`` is a string
    (``"Success"``/``"Killed"``/...) or ``{"Failed": <exit-code>}``.

    A dead ``pueued`` refuses its control socket, so the client exits non-zero; that one case is
    re-raised as :class:`DaemonDown` (``daemon down``) rather than crashing every caller, so a host
    whose queue died reads as unreachable and ``lote revive`` brings it back.
    """
    command = binary(machine, root)[["status", "--json", *(["--group", group] if group else [])]]
    try:
        output = command()
    except ProcessExecutionError as error:
        if daemon_failure(error.stderr or ""):
            raise DaemonDown("daemon down") from error
        raise
    tasks: list[PueueTask] = []
    for task in json.loads(output).get("tasks", {}).values():
        state, fields = next(iter(task["status"].items()))
        result = fields.get("result")
        tasks.append(
            PueueTask(
                id=task["id"],
                label=task.get("label"),
                state=PueueState(state),
                result=next(iter(result)) if isinstance(result, dict) else result,
                exit_code=result.get("Failed")
                if isinstance(result, dict)
                else (0 if result == "Success" else None),
                start=fields.get("start"),
            ),
        )
    return tasks


def log(
    task_id: int | str,
    *,
    machine: Machine = local,
    root: Path | str | None = None,
    lines: int | None = None,
) -> str:
    """Return the captured log of ``task_id`` (last ``lines`` lines, else the full log)."""
    tail = ["--lines", str(lines)] if lines else ["--full"]
    return str(binary(machine, root)[["log", *tail, str(task_id)]]())


def kill(
    task_ids: int | str | Sequence[int | str],
    *,
    machine: Machine = local,
    root: Path | str | None = None,
) -> str:
    """Kill one or many tasks."""
    ids = [task_ids] if isinstance(task_ids, int | str) else task_ids
    return str(binary(machine, root)[["kill", *(str(task_id) for task_id in ids)]]())


def cancel(
    task_id: int | str,
    *,
    machine: Machine = local,
    root: Path | str | None = None,
) -> str:
    """Cancel one task with the operation valid for its current lifecycle state."""
    task = next(
        (item for item in status(machine=machine, root=root) if item.id == int(task_id)), None
    )
    if task is None or task.state == PueueState.DONE:
        return ""
    if task.state in _KILLABLE:
        return kill(task_id, machine=machine, root=root)
    if task.state in _REMOVABLE:
        return remove(task_id, machine=machine, root=root)
    raise ValueError(f"unsupported pueue state {task.state}")


def clean(
    *, machine: Machine = local, root: Path | str | None = None, successful_only: bool = False
) -> str:
    """Drop finished tasks from the list."""
    return str(
        binary(machine, root)[["clean", *(["--successful-only"] if successful_only else [])]]()
    )


def remove(
    task_ids: int | str | Sequence[int | str],
    *,
    machine: Machine = local,
    root: Path | str | None = None,
) -> str:
    """Drop one or many tasks from the list entirely, so each reads as ``vanished`` afterwards.

    pueue only removes tasks that are not currently running (Queued, Stashed, Done), so a caller
    that wants to drop a running task kills it first. This is how :meth:`Pueue.revive` retires the
    zombie tasks a crashed daemon left behind, turning the host's job table honest in one call.
    """
    ids = [task_ids] if isinstance(task_ids, int | str) else task_ids
    return str(binary(machine, root)[["remove", *(str(task_id) for task_id in ids)]]())


def resume(
    *, machine: Machine = local, root: Path | str | None = None, group: str = "default"
) -> str:
    """Set ``group`` back to running so its tasks dispatch again (``pueue start --group``).

    pueue pauses a group when its daemon restarts after a crash, so the requeued tasks do not
    relaunch on their own. Once :meth:`Pueue.revive` has cleared the dead ones, this un-pauses the
    group so the revived host runs new work instead of sitting idle behind a paused queue.
    """
    return str(binary(machine, root)[["start", "--group", group]]())
