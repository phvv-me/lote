from pathlib import Path

import pytest
from hypothesis import given

from lote.environment import Environment
from lote.models import LOGIN, Config, NodeClass, Snapshot, Target
from lote.sync import ALWAYS_EXCLUDE, GitignoreFilter
from lote.targets import resolve, smallest_fit, ssh_hosts

from .strategies import node_classes, snapshots, targets


def gpu_host(name: str, gpu_mem_mb: int) -> Target:
    """A single-login-class GPU target (the old flat shape, now under `classes`)."""
    return Target(name=name, classes={LOGIN: NodeClass(name=LOGIN, gpu_mem_mb=gpu_mem_mb)})


# --- NodeClass memory / arch resolution ---


def test_node_class_vram_prefers_gpu_over_sysmem() -> None:
    """vram_gb uses GPU VRAM when present, else system memory, else None."""
    assert NodeClass(name="q", gpu_mem_mb=40960).vram_gb == 40.0
    assert NodeClass(name="q", sysmem_gb=128).vram_gb == 128.0
    assert NodeClass(name="q").vram_gb is None


def test_node_class_arch_strips_nvidia_prefix() -> None:
    """arch is the GPU name minus `NVIDIA`, or None when no GPU."""
    assert NodeClass(name="q", gpu_name="NVIDIA GB10").arch == "GB10"
    assert NodeClass(name="q").arch is None


@given(node_classes())
def test_node_class_vram_matches_fields(node: NodeClass) -> None:
    """vram_gb mirrors gpu_mem_mb when set, sysmem_gb otherwise, None when neither is."""
    if node.gpu_mem_mb is not None:
        assert node.vram_gb == node.gpu_mem_mb / 1024
    elif node.sysmem_gb is not None:
        assert node.vram_gb == float(node.sysmem_gb)
    else:
        assert node.vram_gb is None


# --- Snapshot -> NodeClass projection ---


@given(snapshots())
def test_snapshot_projects_onto_node_class(snapshot: Snapshot) -> None:
    """node_class carries the first GPU's identity, counts every GPU, and rounds memory."""
    node = snapshot.node_class("debug-g")
    assert node.name == "debug-g"
    assert node.gpu_count == len(snapshot.gpus)
    if snapshot.gpus:
        assert node.gpu_name == (snapshot.gpus[0].unit_name or None)
    else:
        assert node.gpu_name is None and node.gpu_mem_mb is None
    assert node.sysmem_gb == (round(snapshot.memory.total_bytes / 1024**3) or None)
    assert node.cpu_cores == (snapshot.cpu.logical_cores or None)
    assert node.hostname == (snapshot.hostname or None)


def test_snapshot_ignores_unknown_telemetry_fields() -> None:
    """mainboard's extra telemetry (board, toolchain, npus, ...) never breaks the slice."""
    snapshot = Snapshot.model_validate(
        {
            "hostname": "node001",
            "cpu": {"name": "Grace", "logical_cores": 72, "vendor": "arm"},
            "memory": {"total_bytes": 100 * 1024**3, "used_bytes": 1},
            "gpus": [{"unit_name": "NVIDIA H100", "memory": {"total_bytes": 96 * 1024**3}}],
            "board": {"vendor": "x"},
            "npus": [],
            "timestamp_ns": 1,
        }
    )
    node = snapshot.node_class("regular-g")
    assert node.gpu_name == "NVIDIA H100"
    assert node.gpu_mem_mb == 96 * 1024
    assert node.sysmem_gb == 100
    assert node.cpu_cores == 72
    assert node.hostname == "node001"


# --- Target per-class capability resolution ---


def test_target_best_picks_largest_class() -> None:
    """The most capable class drives arch/vram, so GPU queues count over the login node."""
    target = Target(
        name="hpc",
        kind="pbs",
        classes={
            LOGIN: NodeClass(name=LOGIN, sysmem_gb=128),
            "debug-g": NodeClass(name="debug-g", gpu_name="NVIDIA H100", gpu_mem_mb=96 * 1024),
        },
    )
    assert target.best is not None and target.best.name == "debug-g"
    assert target.arch == "H100"
    assert target.vram_gb == 96.0


def test_target_without_classes_has_no_capabilities() -> None:
    """An unprobed target reports no arch and no memory, and fits nothing."""
    target = Target(name="h")
    assert target.best is None and target.arch is None and target.vram_gb is None
    assert not target.fits(0.1)


@given(targets())
def test_target_fits_matches_vram(target: Target) -> None:
    """fits(n) is exactly `vram_gb is not None and vram_gb >= n` for any target."""
    if target.vram_gb is None:
        assert not target.fits(1.0)
    else:
        assert target.fits(target.vram_gb)
        assert not target.fits(target.vram_gb + 1)


@given(targets())
def test_target_vram_is_the_best_class_vram(target: Target) -> None:
    """vram_gb is the largest GPU class's memory, else the largest class's memory."""
    sized = [node for node in target.classes.values() if node.vram_gb is not None]
    gpu = [node.vram_gb for node in sized if node.gpu_name is not None]
    everything = [node.vram_gb for node in sized]
    expected = max(gpu) if gpu else (max(everything) if everything else None)
    assert target.vram_gb == expected


def test_target_environment_carries_root_and_login_choice() -> None:
    """`environment` builds the activation context from the target's root and login-shell flag."""
    env = Target(name="h", root="/work", login_shell=False).environment
    assert env.root == "/work" and env.login is False


@pytest.mark.parametrize(
    ("login", "flag"),
    [(True, "-lc"), (False, "-c")],
)
def test_environment_argv_picks_shell_flag(login: bool, flag: str) -> None:
    """`argv` wraps the body under a login (`-lc`) or plain (`-c`) bash shell."""
    argv = Environment(root="/repo", login=login).argv("echo hi", chefe=False)
    assert argv[:2] == ["bash", flag]
    assert argv[2].endswith("echo hi")


# --- smallest_fit selection ---


def test_smallest_fit_picks_smallest_sufficient() -> None:
    """Among fitting targets, the smallest-VRAM one wins (keeps big iron free)."""
    small = gpu_host("small", 16 * 1024)
    medium = gpu_host("medium", 40 * 1024)
    big = gpu_host("big", 80 * 1024)
    assert smallest_fit([big, small, medium], 20.0) is medium


def test_smallest_fit_raises_when_none_fit() -> None:
    """No target with enough VRAM is a LookupError listing what's available."""
    with pytest.raises(LookupError, match="no target fits"):
        smallest_fit([gpu_host("tiny", 8 * 1024)], 100.0)


# --- resolve applies hints over probe facts ---


def test_resolve_overlays_hints_on_facts() -> None:
    """`[hints.<alias>]` keys override the probe facts; the rest of the facts pass through."""
    config = Config().model_copy(update={"hints": {"dgx": {"kind": "slurm", "queue": "gpu"}}})
    facts = Target(name="dgx", kind="ssh", root="/work")
    target = resolve("dgx", config, facts)
    assert target.kind == "slurm" and target.queue == "gpu" and target.root == "/work"


def test_resolve_rejects_typoed_hint_keys() -> None:
    """A hint key that is not a Target field fails loudly, naming the bad key and the table."""
    config = Config().model_copy(update={"hints": {"dgx": {"knid": "slurm"}}})
    with pytest.raises(LookupError, match=r"knid.*\[hints\.dgx\]"):
        resolve("dgx", config, Target(name="dgx"))


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


def test_ssh_hosts_accepts_tabs_and_skips_include(tmp_path) -> None:  # noqa: ANN001
    """A tab-separated `Host\\tname` parses like a spaced one; Include directives are ignored."""
    config = tmp_path / "config"
    config.write_text("Include ~/.ssh/extra\nHost\tgold\n\tHostName 1.2.3.4\nHost \t miyabi\n")
    assert ssh_hosts(config) == ["gold", "miyabi"]


def test_ssh_hosts_missing_file_is_empty(tmp_path) -> None:  # noqa: ANN001
    """A nonexistent ssh config yields no hosts."""
    assert ssh_hosts(tmp_path / "absent") == []


# --- GitignoreFilter ---


def test_gitignore_filter_excludes_and_matches(tmp_path) -> None:  # noqa: ANN001
    """Root and nested merge rules drive rsync while pathspec drives `ignored`."""
    (tmp_path / ".gitignore").write_text("# a comment\n\n__pycache__/\n*.log\n!keep.log\nbuild/\n")
    gitignore = GitignoreFilter(tmp_path)
    assert gitignore.filters == ["merge,- .gitignore", ":- .gitignore"]
    assert gitignore.excludes == list(ALWAYS_EXCLUDE)
    assert gitignore.ignored("build/x") is True
    assert gitignore.ignored(tmp_path / "src" / "a.log") is True  # absolute, under root
    assert gitignore.ignored("src/main.py") is False


def test_gitignore_filter_missing_gitignore(tmp_path) -> None:  # noqa: ANN001
    """No root file still enables nested merge files and keeps the fixed excludes."""
    gitignore = GitignoreFilter(tmp_path)
    assert gitignore.filters == [":- .gitignore"]
    assert gitignore.excludes == list(ALWAYS_EXCLUDE)
    assert gitignore.ignored("anything.py") is False


def test_gitignore_filter_finds_control_files_above_narrow_sources(tmp_path: Path) -> None:
    """Existing ancestor ignore files are ordered root first and deduplicated."""
    (tmp_path / ".gitignore").write_text("*.scratch\n")
    (tmp_path / "research").mkdir()
    (tmp_path / "research/.gitignore").write_text("generated/\n")
    (tmp_path / "research/projects").mkdir()
    gitignore = GitignoreFilter(tmp_path)

    assert gitignore.control_files(
        ["research/projects", "research/projects/run.py", str(tmp_path / "research/projects")]
    ) == [".gitignore", "research/.gitignore"]


def test_gitignore_filter_excludes_submodule_git_file() -> None:
    """Submodule internals and secret environment files are always excluded."""
    assert ".git" in ALWAYS_EXCLUDE
    assert ".git/" not in ALWAYS_EXCLUDE  # a slash would match only the directory
    assert ".env" in ALWAYS_EXCLUDE
