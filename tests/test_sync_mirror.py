"""Mirror semantics against the real rsync binary, local dir to local dir.

`_rsync_up` ships with `--delete` plus the `[sync].protect` filters; these tests
exercise that exact flag combination end-to-end so the pruning and protection
behavior is pinned by rsync itself, not by argv assertions alone.
"""

from contextlib import chdir
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lote import STATE_DIR
from lote.clients.rsync import Rsync, rsync
from lote.models import Sync
from lote.sync import GitignoreFilter

# The exact transfer mode `_rsync_up` uses (compression is irrelevant locally).
MIRROR = Rsync.ARCHIVE | Rsync.RELATIVE | Rsync.DELETE | Rsync.DELETE_AFTER

# Short lowercase identifiers for generated file stems.
STEMS = st.text("abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=12)


def seed(root: Path, *files: str) -> None:
    """Create each relative path under `root` (parents included) with token content."""
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)


@given(
    stale=st.sets(STEMS, min_size=1, max_size=4),
    results=st.sets(STEMS, min_size=1, max_size=4),
)
def test_mirror_prunes_stale_files_but_never_results(
    tmp_path_factory: pytest.TempPathFactory, stale: set[str], results: set[str]
) -> None:
    """Any host-only file under a synced tree is pruned, while any file under a
    `results/` dir survives via the default protect patterns."""
    base = tmp_path_factory.mktemp("mirror")
    repo, host = base / "repo", base / "host"
    seed(repo, "research/src/new.py", "research/experiments/e1/run.py")
    seed(host, *(f"research/src/stale_{stem}.py" for stem in stale))
    seed(host, *(f"research/experiments/e1/results/{stem}.json" for stem in results))
    with chdir(repo):
        rsync(["research"], f"{host}/", MIRROR, protect=Sync().protect)
    assert (host / "research/src/new.py").is_file()
    assert not any((host / f"research/src/stale_{stem}.py").exists() for stem in stale)
    kept = (host / f"research/experiments/e1/results/{stem}.json" for stem in results)
    assert all(path.is_file() for path in kept)


def test_stale_package_dir_no_longer_shadows_its_module(tmp_path: Path) -> None:
    """The recurring breakage: a `lattice/` package became `lattice.py` locally, and
    without pruning the stale host directory kept shadowing the new module."""
    repo, host = tmp_path / "repo", tmp_path / "host"
    seed(repo, "research/quant/methods/lattice.py")
    seed(host, "research/quant/methods/lattice/__init__.py")
    with chdir(repo):
        rsync(["research"], f"{host}/", MIRROR, protect=Sync().protect)
    assert (host / "research/quant/methods/lattice.py").is_file()
    assert not (host / "research/quant/methods/lattice").exists()


def test_host_only_results_logs_and_state_survive_pruning(tmp_path: Path) -> None:
    """Remote-only experiment results, log trees, loose logs and lote's state all
    outlive a mirroring sync that prunes everything else host-only."""
    repo, host = tmp_path / "repo", tmp_path / "host"
    seed(repo, "research/projects/x/experiments/e1/run.py")
    seed(
        host,
        "research/projects/x/experiments/e1/results/part-0.parquet",
        "research/projects/x/experiments/e1/logs/job.log",
        "research/projects/x/experiments/e1/stdout.log",
        "research/projects/x/experiments/e1/leftover.json",
        f"{STATE_DIR}/db.json",
    )
    with chdir(repo):
        rsync(["research"], f"{host}/", MIRROR, protect=Sync().protect)
    experiment = host / "research/projects/x/experiments/e1"
    assert (experiment / "results/part-0.parquet").is_file()
    assert (experiment / "logs/job.log").is_file()
    assert (experiment / "stdout.log").is_file()
    assert not (experiment / "leftover.json").exists()  # unprotected host-only file goes
    assert (host / STATE_DIR / "db.json").is_file()


def test_excluded_host_artifacts_are_never_deleted(tmp_path: Path) -> None:
    """Plain `--delete` leaves excluded receiver paths alone, so the gitignore-derived
    denylist keeps shielding host-side envs and caches even under pruning."""
    repo, host = tmp_path / "repo", tmp_path / "host"
    seed(repo, "research/run.py")
    seed(host, "research/.pixi/env/bin/python", "research/gone.py")
    with chdir(repo):
        rsync(["research"], f"{host}/", MIRROR, exclude=[".pixi/"], protect=Sync().protect)
    assert (host / "research/.pixi/env/bin/python").is_file()
    assert not (host / "research/gone.py").exists()


def test_gitignored_files_are_never_shipped_or_deleted(tmp_path: Path) -> None:
    """Root and nested Git ignores protect both sides of a mirroring sync."""
    repo, host = tmp_path / "repo", tmp_path / "host"
    seed(
        repo,
        ".gitignore",
        "research/.gitignore",
        "research/run.py",
        "research/local.scratch",
        "research/generated/local-summary.json",
    )
    (repo / ".gitignore").write_text("*.scratch\n")
    (repo / "research/.gitignore").write_text("generated/\n")
    seed(
        host,
        "research/host-only.scratch",
        "research/generated/host-summary.parquet",
        "research/stale.py",
    )

    with chdir(repo):
        rsync(
            ["research"],
            f"{host}/",
            MIRROR,
            filters=["merge,- .gitignore", ":- .gitignore"],
        )

    assert (host / "research/run.py").is_file()
    assert not (host / "research/local.scratch").exists()
    assert not (host / "research/generated/local-summary.json").exists()
    assert (host / "research/host-only.scratch").is_file()
    assert (host / "research/generated/host-summary.parquet").is_file()
    assert not (host / "research/stale.py").exists()


def test_parent_gitignore_protects_a_narrow_included_subtree(tmp_path: Path) -> None:
    """An ignore file above a source path still protects its host-side artifacts."""
    repo, host = tmp_path / "repo", tmp_path / "host"
    seed(
        repo,
        ".gitignore",
        "research/.gitignore",
        "research/projects/run.py",
        "research/projects/generated/local-summary.json",
    )
    (repo / ".gitignore").write_text("*.scratch\n")
    (repo / "research/.gitignore").write_text("generated/\n")
    seed(
        host,
        "research/projects/generated/host-summary.parquet",
        "research/projects/stale.py",
    )

    with chdir(repo):
        gitignore = GitignoreFilter(repo)
        rsync(
            ["research/projects", *gitignore.control_files(["research/projects"])],
            f"{host}/",
            MIRROR,
            filters=gitignore.filters,
        )

    assert (host / "research/projects/run.py").is_file()
    assert not (host / "research/projects/generated/local-summary.json").exists()
    assert (host / "research/projects/generated/host-summary.parquet").is_file()
    assert not (host / "research/projects/stale.py").exists()


def test_sync_never_sends_or_replaces_nested_secret_env_files(tmp_path: Path) -> None:
    """A package-local `.env` remains host-owned even when a superproject sync includes
    the whole package and its root gitignore cannot describe the submodule's ignores."""
    repo, host = tmp_path / "repo", tmp_path / "host"
    seed(repo, "packages/aizk/app.py", "packages/aizk/.env")
    seed(host, "packages/aizk/.env")
    local_secret = repo / "packages/aizk/.env"
    remote_secret = host / "packages/aizk/.env"
    local_secret.write_text("LOCAL=wrong")
    remote_secret.write_text("REMOTE=right")

    with chdir(repo):
        rsync(
            ["packages/aizk"],
            f"{host}/",
            MIRROR,
            exclude=[".env"],
            protect=Sync().protect,
        )

    assert (host / "packages/aizk/app.py").is_file()
    assert remote_secret.read_text() == "REMOTE=right"


def test_job_script_extra_arg_ships_without_pruning_state(tmp_path: Path) -> None:
    """A `.lote/jobs` script passed as an extra source lands on the host while the
    host's own state directory (old scripts, the run db) is left untouched."""
    repo, host = tmp_path / "repo", tmp_path / "host"
    seed(repo, "research/run.py", f"{STATE_DIR}/jobs/job-new.sh")
    seed(host, f"{STATE_DIR}/jobs/job-old.sh", f"{STATE_DIR}/db.json")
    with chdir(repo):
        rsync(
            ["research", f"{STATE_DIR}/jobs/job-new.sh"],
            f"{host}/",
            MIRROR,
            exclude=[f"{STATE_DIR}/"],
            protect=Sync().protect,
        )
    assert (host / STATE_DIR / "jobs/job-new.sh").is_file()
    assert (host / STATE_DIR / "jobs/job-old.sh").is_file()
    assert (host / STATE_DIR / "db.json").is_file()


def test_sync_protect_defaults_cover_state_results_and_logs() -> None:
    """The defaults shield lote state, result/log trees, and the in-place measured artifacts."""
    assert Sync().protect == [
        f"{STATE_DIR}/***",
        "results/***",
        "logs/***",
        "*.log",
        "evidence/***",
        "*_vector-*.json",
        "info_organization-*.json",
        "*_result*.json",
    ]
