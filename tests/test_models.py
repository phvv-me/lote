from __future__ import annotations

import math

import pytest
from hypothesis import given

from fleet.models import Config, Target
from fleet.sync import ALWAYS_EXCLUDE, GitignoreFilter
from fleet.targets import resolve, smallest_fit, ssh_hosts

from .strategies import targets

# --- Target memory / arch resolution ---


def test_target_vram_prefers_gpu_over_sysmem() -> None:
    """vram_gb uses GPU VRAM when present, else system memory, else None."""
    assert Target(name="h", gpu_mem_mb=40960).vram_gb == 40.0
    assert Target(name="h", sysmem_gb=128).vram_gb == 128.0
    assert Target(name="h").vram_gb is None


def test_target_arch_strips_nvidia_prefix() -> None:
    """arch is the GPU name minus `NVIDIA`, or None when no GPU."""
    assert Target(name="h", gpu_name="NVIDIA GB10").arch == "GB10"
    assert Target(name="h").arch is None


@given(targets())
def test_target_fits_matches_vram(target: Target) -> None:
    """fits(n) is exactly `vram_gb is not None and vram_gb >= n` for any target."""
    if target.vram_gb is None:
        assert not target.fits(1.0)
    else:
        assert target.fits(target.vram_gb)
        assert not target.fits(target.vram_gb + 1)


# --- smallest_fit selection ---


def test_smallest_fit_picks_smallest_sufficient() -> None:
    """Among fitting targets, the smallest-VRAM one wins (keeps big iron free)."""
    small = Target(name="small", gpu_mem_mb=16 * 1024)
    medium = Target(name="medium", gpu_mem_mb=40 * 1024)
    big = Target(name="big", gpu_mem_mb=80 * 1024)
    assert smallest_fit([big, small, medium], 20.0) is medium


def test_smallest_fit_raises_when_none_fit() -> None:
    """No target with enough VRAM is a SystemExit listing what's available."""
    with pytest.raises(SystemExit, match="no target fits"):
        smallest_fit([Target(name="tiny", gpu_mem_mb=8 * 1024)], 100.0)


# --- resolve applies hints over probe facts ---


def test_resolve_overlays_hints_on_facts() -> None:
    """`[hints.<alias>]` keys override the probe facts; unknown facts keys are ignored."""
    config = Config().model_copy(update={"hints": {"dgx": {"kind": "slurm", "queue": "gpu"}}})
    facts = {"name": "dgx", "kind": "ssh", "root": "/work", "extraneous": 1}
    target = resolve("dgx", config, facts)
    assert target.kind == "slurm" and target.queue == "gpu" and target.root == "/work"


# --- ssh_hosts parsing ---


def test_ssh_hosts_splits_aliases_and_drops_patterns(tmp_path) -> None:  # noqa: ANN001
    """Multi-alias Host lines split; wildcard tokens drop; order and dedup preserved."""
    config = tmp_path / "config"
    config.write_text(
        "Host dgx spark\n  HostName 1.2.3.4\n"
        "Host *\n  User me\n"
        "Host dl*\n"
        "Host hpc\n"
        "Host dgx\n"  # duplicate, dropped
    )
    assert ssh_hosts(config) == ["dgx", "spark", "hpc"]


def test_ssh_hosts_missing_file_is_empty(tmp_path) -> None:  # noqa: ANN001
    """A nonexistent ssh config yields no hosts."""
    assert ssh_hosts(tmp_path / "absent") == []


# --- GitignoreFilter ---


def test_gitignore_filter_excludes_and_matches(tmp_path) -> None:  # noqa: ANN001
    """Patterns become rsync excludes (negations/comments dropped) and drive `ignored`."""
    (tmp_path / ".gitignore").write_text("# a comment\n\n__pycache__/\n*.log\n!keep.log\nbuild/\n")
    gitignore = GitignoreFilter(tmp_path)
    assert set(ALWAYS_EXCLUDE) <= set(gitignore.excludes)
    assert "*.log" in gitignore.excludes
    assert "!keep.log" not in gitignore.excludes  # negations can't translate to rsync
    assert gitignore.ignored("build/x") is True
    assert gitignore.ignored(tmp_path / "src" / "a.log") is True  # absolute, under root
    assert gitignore.ignored("src/main.py") is False


def test_gitignore_filter_missing_gitignore(tmp_path) -> None:  # noqa: ANN001
    """No .gitignore still yields the always-excludes and ignores nothing user-listed."""
    gitignore = GitignoreFilter(tmp_path)
    assert gitignore.excludes == list(ALWAYS_EXCLUDE)
    assert gitignore.ignored("anything.py") is False


# Touch math so an unused-import lint never sneaks in (vram is a float ratio).
def test_vram_is_exact_ratio() -> None:
    """VRAM in GB is MiB / 1024 exactly."""
    assert math.isclose(Target(name="h", gpu_mem_mb=81920).vram_gb, 80.0)
