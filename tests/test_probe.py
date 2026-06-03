from __future__ import annotations

import json

import pytest

import fleet.probe as probe_mod
from fleet.models import Target
from fleet.probe import gpu, interactive_queue, probe, stdout


def fake_group(name: str):  # noqa: ANN202
    """A stand-in for grp.getgrgid's return (only `.gr_name` is read)."""
    return type("G", (), {"gr_name": name})


def test_stdout_swallows_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout returns the process output, and `""` when the binary is missing (OSError)."""
    monkeypatch.setattr(
        probe_mod.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "hi\n"})
    )
    assert stdout("echo", "hi") == "hi\n"

    def boom(*_a, **_k):  # noqa: ANN202
        raise OSError("no such tool")

    monkeypatch.setattr(probe_mod.subprocess, "run", boom)
    assert stdout("nvidia-smi") == ""


def test_gpu_parses_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """gpu() splits the first nvidia-smi csv line into (name, memory_MiB)."""
    monkeypatch.setattr(probe_mod, "stdout", lambda *_: "NVIDIA A100, 81920\nNVIDIA A100, 81920\n")
    assert gpu() == ("NVIDIA A100", 81920)


def test_gpu_absent_is_none_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """No nvidia-smi output (no GPU) yields (None, None); a non-numeric mem yields a None mem."""
    monkeypatch.setattr(probe_mod, "stdout", lambda *_: "   \n")
    assert gpu() == (None, None)
    monkeypatch.setattr(probe_mod, "stdout", lambda *_: "NVIDIA GB10, N/A\n")
    assert gpu() == ("NVIDIA GB10", None)


def test_interactive_queue_only_for_pbs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-PBS hosts have no interactive queue; a PBS host picks the queue named `interact`."""
    assert interactive_queue("ssh") is None
    qstat_q = "Queue\n-----\nregular max\ninteractive max\ngpu max\n"
    monkeypatch.setattr(probe_mod, "stdout", lambda *_: qstat_q)
    assert interactive_queue("pbs") == "interactive"


def test_interactive_queue_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PBS host without an interactive queue resolves to None."""
    monkeypatch.setattr(probe_mod, "stdout", lambda *_: "Queue\n-----\nregular max\ngpu max\n")
    assert interactive_queue("pbs") is None


@pytest.mark.parametrize(
    ("which_map", "expected_kind"),
    [
        ({"sbatch": "/x/sbatch"}, "slurm"),
        ({"qsub": "/x/qsub"}, "pbs"),
        ({}, "ssh"),
    ],
)
def test_probe_emits_target_json(
    which_map: dict[str, str],
    expected_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """probe prints a Target JSON whose kind follows the scheduler on PATH, with gpu/mem/group."""
    monkeypatch.setattr(probe_mod.shutil, "which", lambda name: which_map.get(name))
    monkeypatch.setattr(probe_mod, "gpu", lambda: ("NVIDIA H100", 81920))
    monkeypatch.setattr(probe_mod, "interactive_queue", lambda _kind: None)
    monkeypatch.setattr(
        probe_mod.psutil, "virtual_memory", lambda: type("M", (), {"total": 64 * 1024**3})
    )
    monkeypatch.setattr(probe_mod.os, "getgid", lambda: 0)
    monkeypatch.setattr(probe_mod.grp, "getgrgid", lambda _gid: fake_group("research"))

    probe("spark", "/work/u/projects")

    payload = json.loads(capsys.readouterr().out)
    target = Target.model_validate(payload)
    assert target.name == "spark"
    assert target.root == "/work/u/projects"
    assert target.kind == expected_kind
    assert target.gpu_name == "NVIDIA H100"
    assert target.gpu_mem_mb == 81920
    assert target.sysmem_gb == 64
    assert target.account == "research"
