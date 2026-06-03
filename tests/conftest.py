from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from plumbum import local
from rich.console import Console


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
        extra = list(args) if isinstance(args, (list, tuple)) else [args]
        child = RecordingCommand(self.name, self.calls, self.outputs)
        child.bound = [*self.bound, *(str(a) for a in extra)]
        return child

    def __call__(self, *_: object, **__: object) -> str:
        self.calls.append([self.name, *self.bound])
        return self.outputs.pop(0) if self.outputs else ""

    def __and__(self, _other: object) -> str:  # `command & FG`
        return self.__call__()

    def __lshift__(self, _stdin: object) -> RecordingCommand:  # `command << stdin`
        return self


class RecordingMachine:
    """A fake plumbum machine: `machine["cmd"]` yields a `RecordingCommand`.

    Carries an `env.path` list and a `cwd` so the connect-time PATH insert and `find_root`
    paths in production keep working against the double.
    """

    def __init__(self, outputs: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.outputs = list(outputs or [])

    def __getitem__(self, name: str) -> RecordingCommand:
        return RecordingCommand(name, self.calls, self.outputs)


@pytest.fixture
def remote() -> RecordingMachine:
    """A recording fake `SshMachine`/`local` for scheduler command-construction tests."""
    return RecordingMachine()
