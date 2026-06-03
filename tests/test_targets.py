from __future__ import annotations

import json

import pytest

from lote.targets import find_root, probe_host

from .conftest import RecordingMachine


def test_find_root_runs_root_finder() -> None:
    """find_root runs the ROOT_FINDER snippet via `bash -lc` and returns its stripped stdout."""
    machine = RecordingMachine([" /work/grp/u/projects \n"])
    assert find_root(machine) == "/work/grp/u/projects"
    [call] = machine.calls
    assert call[:2] == ["bash", "-lc"]


def test_probe_host_runs_in_env_probe_and_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """probe_host runs the chefe-wrapped probe in a login shell and json-loads the Target."""
    payload = {"name": "spark", "kind": "ssh", "root": "/repo"}
    machine = RecordingMachine([json.dumps(payload)])
    result = probe_host(machine, "spark", "/repo")
    assert result == payload
    inner = machine.calls[0][2]
    assert inner.startswith("cd /repo &&")
    assert "lote.probe spark /repo" in inner
