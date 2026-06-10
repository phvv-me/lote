from __future__ import annotations

from lote.targets import find_root, probe_capabilities

from .conftest import RecordingMachine


def test_find_root_runs_root_finder() -> None:
    """find_root runs the ROOT_FINDER snippet via `bash -lc` and returns its stripped stdout."""
    machine = RecordingMachine([" /work/grp/u/projects \n"])
    assert find_root(machine) == "/work/grp/u/projects"
    [call] = machine.calls
    assert call[:2] == ["bash", "-lc"]


def test_probe_capabilities_parses_key_value_lines() -> None:
    """probe_capabilities runs the stock-tool script in a login shell and builds a Target."""
    raw = (
        "root=/work/u/projects\nkind=pbs\ngpu=NVIDIA H100, 81920\n"
        "mem=263842992\naccount=research\nqueue=interactive\n"
    )
    machine = RecordingMachine([raw])
    facts = probe_capabilities(machine, "spark")
    assert facts.name == "spark"
    assert facts.root == "/work/u/projects"
    assert facts.kind == "pbs"
    assert facts.gpu_name == "NVIDIA H100"
    assert facts.gpu_mem_mb == 81920
    assert facts.sysmem_gb == round(263842992 / 1024**2)
    assert facts.account == "research"
    assert facts.queue == "interactive"
    [call] = machine.calls
    assert call[:2] == ["bash", "-lc"]


def test_probe_capabilities_handles_absent_gpu_memory_and_queue() -> None:
    """An empty gpu/mem/queue (no GPU, unreadable meminfo, no PBS) resolves each to None."""
    raw = "root=/home/u/projects\nkind=ssh\ngpu=\nmem=\naccount=u\nqueue=\n"
    facts = probe_capabilities(RecordingMachine([raw]), "box")
    assert facts.gpu_name is None
    assert facts.gpu_mem_mb is None
    assert facts.sysmem_gb is None
    assert facts.queue is None


def test_probe_capabilities_gb10_unified_memory_falls_back_to_sysmem() -> None:
    """A GB10 reports no VRAM number (`[N/A]`), so usable memory comes from system RAM."""
    raw = "root=/r\nkind=ssh\ngpu=NVIDIA GB10, [N/A]\nmem=131334144\naccount=u\nqueue=\n"
    facts = probe_capabilities(RecordingMachine([raw]), "spark")
    assert facts.gpu_name == "NVIDIA GB10"
    assert facts.gpu_mem_mb is None
    assert facts.sysmem_gb == round(131334144 / 1024**2)
