import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from plumbum import local
from rich.console import Console

from lote.cache import RunRecord, ServiceRecord
from lote.models import LOGIN, NodeClass, Snapshot, Target
from lote.schedulers import JobState

# Several property-based tests shell out (rsync, stubbed schedulers), whose first
# cold run routinely overshoots hypothesis's 200ms default deadline and flakes the
# suite; wall-clock timing is not a property these tests assert, so it is off.
settings.register_profile("lote", deadline=None)
settings.load_profile("lote")


def fake_group(name: str) -> type:
    """A stand-in for `grp.getgrgid`'s return (only `.gr_name` is read)."""
    return type("Group", (), {"gr_name": name})


@pytest.fixture
def console() -> Console:
    """A Rich console pinned to 80 columns and no color so rendered tables snapshot stably."""
    return Console(width=80, force_terminal=False, color_system=None, legacy_windows=False)


@pytest.fixture
def stub_bin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """Stub the scheduler/transport executables on plumbum's PATH so `local["cmd"]` resolves.

    `pytest-subprocess` intercepts the real invocation; this only makes plumbum's `which`
    succeed. Yields each tool's resolved absolute path (what plumbum runs, so what a fake
    must register).
    """
    bindir = tmp_path_factory.mktemp("bin")
    paths: dict[str, str] = {}
    for tool in (
        "qstat",
        "qsub",
        "qdel",
        "sacct",
        "squeue",
        "sbatch",
        "scancel",
        "pueue",
        "rsync",
    ):
        executable = bindir / tool
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
        paths[tool] = str(executable)
    with local.env(PATH=f"{bindir}{os.pathsep}{local.env['PATH']}"):
        yield paths


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the body in a fresh empty CWD so cache/history files and globs stay hermetic."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class RecordingCommand:
    """A plumbum-command stand-in that records its argv and replays a canned stdout.

    `remote["bash"][["-lc", cmd]]` indexes a command then bounds args; calling it (or
    `& FG`, or `(retcode=None)`) runs it. This double records every bound argv into a
    shared list and returns the queued stdout for the matching call, so a scheduler test
    asserts the exact command string built without any real process or ssh.
    """

    def __init__(self, name: str, calls: list[list[str]], outputs: list[str]) -> None:
        self.name = name
        self.calls = calls
        self.outputs = outputs
        self.bound: list[str] = []

    def __getitem__(self, args: object) -> RecordingCommand:
        extra = list(args) if isinstance(args, list | tuple) else [args]
        child = RecordingCommand(self.name, self.calls, self.outputs)
        child.bound = [*self.bound, *(str(a) for a in extra)]
        return child

    def __call__(self, *_: object, **__: object) -> str:
        self.calls.append([self.name, *self.bound])
        return self.outputs.pop(0) if self.outputs else ""

    def run(self, *_: object, **__: object) -> tuple[int, str, str]:  # `command.run(retcode=None)`
        return (0, self.__call__(), "")

    def __and__(self, _other: object) -> str:  # `command & FG`
        return self.__call__()

    def __lshift__(self, _stdin: object) -> RecordingCommand:  # `command << stdin`
        return self


class RecordingMachine:
    """A fake plumbum machine: `machine["cmd"]` yields a `RecordingCommand`.

    Carries an `env.path` list and a `cwd` so the connect-time PATH insert and `find_root`
    paths in production keep working against the double.
    """

    def __init__(self, outputs: list[str] | None = None, env_pueue: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.outputs = list(outputs or [])
        self.env_pueue = env_pueue

    def __getitem__(self, name: str) -> RecordingCommand:
        return RecordingCommand(name, self.calls, self.outputs)

    def path(self, base: object) -> RecordingPath:
        """Mirror plumbum's machine.path: a host path the pueue client probes for the env copy."""
        return RecordingPath(str(base), exists=self.env_pueue)


class RecordingPath:
    """A fake host path: joins like plumbum's and reports a fixed existence."""

    def __init__(self, base: str, exists: bool) -> None:
        self.base = base
        self.present = exists

    def __truediv__(self, other: str) -> RecordingPath:
        return RecordingPath(f"{self.base}/{other}", self.present)

    def __str__(self) -> str:
        return self.base

    def exists(self) -> bool:
        return self.present


def machine_with(*outputs: str) -> RecordingMachine:
    """A recording machine queued with these stdout strings, one per command call."""
    return RecordingMachine(list(outputs))


@pytest.fixture
def remote() -> RecordingMachine:
    """A recording fake `SshMachine`/`local` for scheduler command-construction tests."""
    return RecordingMachine()


@pytest.fixture
def recorder() -> Console:
    """A capture console pinned to 80 columns so a table render snapshots as stable text."""
    return Console(width=80, record=True, color_system=None, legacy_windows=False)


class FakeRemote:
    """A context-manager stand-in for what the SshMachine `connect()` returns.

    `with connect(name) as remote:` only needs __enter__/__exit__; the scheduler double
    ignores the remote entirely, so this stays empty unless a test wires `__getitem__`.
    """

    def __enter__(self) -> FakeRemote:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


class RecordingScheduler:
    """A `Scheduler` double recording each call and replaying canned results.

    `pick(machine)` is patched to return this, so every CLI command that delegates to a
    backend is observed without ssh or a real scheduler.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.submit_handle = "H1"
        self.state_result = JobState(handle="H1", state="F", exit_code=0, verdict="ok")
        self.queue_list: list[str] = []
        self.revive_cleared: list[str] = []  # the zombie handles a revive reports having cleared

    def submit(self, remote, root, script, args, *, resources) -> str:  # noqa: ANN001
        self.calls.append(("submit", (root, script, tuple(args))))
        self.submit_resources = resources  # the last `Resources` a submit handed the backend
        return self.submit_handle

    def status(self, remote, root) -> None:  # noqa: ANN001
        self.calls.append(("status", (root,)))

    def jobs(self, remote, root) -> list[JobState]:  # noqa: ANN001
        self.calls.append(("jobs", (root,)))
        return [self.state_result]

    def logs(self, remote, root, handle) -> None:  # noqa: ANN001
        self.calls.append(("logs", (root, handle)))

    def state(self, remote, root, handle) -> JobState:  # noqa: ANN001
        self.calls.append(("state", (root, handle)))
        return self.state_result

    def states(self, remote, root, handles) -> dict[str, JobState]:  # noqa: ANN001
        self.calls.append(("states", (root, tuple(handles))))
        return {self.state_result.handle: self.state_result}

    def wait(self, remote, root, handle) -> JobState:  # noqa: ANN001
        self.calls.append(("wait", (root, handle)))
        return self.state_result

    def stream(self, remote, root, handle) -> JobState:  # noqa: ANN001
        self.calls.append(("stream", (root, handle)))
        return self.state_result

    def cancel(self, remote, root, handle) -> None:  # noqa: ANN001
        self.calls.append(("cancel", (root, handle)))

    def revive(self, remote, root) -> list[str]:  # noqa: ANN001
        self.calls.append(("revive", (root,)))
        return self.revive_cleared

    def queues(self, remote, root) -> list[str]:  # noqa: ANN001
        self.calls.append(("queues", (root,)))
        return self.queue_list


# A resolved GB10 target reused as the canonical onboarded host across CLI tests.
GB10 = Target(
    name="spark",
    kind="ssh",
    root="/repo",
    classes={LOGIN: NodeClass(name=LOGIN, gpu_name="NVIDIA GB10", gpu_mem_mb=120 * 1024)},
)


def make_run(
    handle: str,
    *,
    target: str = "dgx",
    script: str = "a.sh",
    submitted_at: str = "t0",
    fetch_path: str | None = None,
) -> RunRecord:
    """A fully-populated `RunRecord` for cache/CLI tests (only the varied fields are args)."""
    return RunRecord(
        handle=handle,
        target=target,
        kind="ssh",
        script=script,
        args="",
        git_sha="abc1234",
        dirty=0,
        submitted_at=submitted_at,
        fetch_path=fetch_path,
    )


def make_service(
    name: str = "vllm",
    *,
    target: str = "gold",
    root: str = "/repo",
    port: int = 8000,
    local_port: int = 8000,
    health_path: str = "/health",
    remote_task: str = "3",
    tunnel_task: str = "1",
) -> ServiceRecord:
    """A fully-populated `ServiceRecord` for cache/services/CLI tests (only the varied fields
    are args)."""
    return ServiceRecord(
        name=name,
        target=target,
        root=root,
        cmd="vllm serve model --port 8000",
        port=port,
        local_port=local_port,
        health_path=health_path,
        remote_task=remote_task,
        tunnel_task=tunnel_task,
        started_at="2024-01-01T00:00:00",
    )


# One H100 node's mainboard snapshot, shared as the probe job's captured log across the
# nodes/models/cli probe tests. `SNAPSHOT_LOG` is the JSON line a probe job prints.
H100_SNAPSHOT_DICT: dict[str, Any] = {
    "hostname": "node001",
    "cpu": {"name": "Grace", "logical_cores": 72},
    "memory": {"total_bytes": 100 * 1024**3},
    "gpus": [{"unit_name": "NVIDIA H100", "memory": {"total_bytes": 96 * 1024**3}}],
}
H100_SNAPSHOT = Snapshot.model_validate(H100_SNAPSHOT_DICT)
SNAPSHOT_LOG = json.dumps(H100_SNAPSHOT_DICT)
