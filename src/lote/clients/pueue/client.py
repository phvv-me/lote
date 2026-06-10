import json
from collections.abc import Sequence
from pathlib import Path

from plumbum import local
from plumbum.commands.base import BaseCommand

from ..machine import Machine
from .state import PueueState
from .task import PueueTask

# pueue ships in the compiled chefe env (chefe.toml [deps]), not in a user bin dir, so a
# bare `pueue` is absent from a fresh host's PATH. The env copy under the repo root wins;
# PATH stays as the fallback for hosts that still carry a cargo-installed pueue.
ENV_PUEUE = ".chefe/.pixi/envs/default/bin/pueue"


def binary(machine: Machine, root: Path | str | None = None) -> BaseCommand:
    """The `pueue` command on ``machine``: the chefe env copy under ``root``, else PATH's."""
    if root is not None:
        env_pueue = machine.path(str(root)) / ENV_PUEUE
        if env_pueue.exists():
            return machine[str(env_pueue)]
    return machine["pueue"]


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
    """
    output = binary(machine, root)[["status", "--json", *(["--group", group] if group else [])]]()
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
    ids = [task_ids] if isinstance(task_ids, (int, str)) else task_ids
    return str(binary(machine, root)[["kill", *(str(task_id) for task_id in ids)]]())


def clean(
    *, machine: Machine = local, root: Path | str | None = None, successful_only: bool = False
) -> str:
    """Drop finished tasks from the list."""
    return str(
        binary(machine, root)[["clean", *(["--successful-only"] if successful_only else [])]]()
    )
