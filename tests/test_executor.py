from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lote.executor.cli import (
    _int_or_none,
    _parse_pbs_directives,
    _parse_sbatch_directives,
    _resolve_jid_or_name,
    _resolve_script,
)

from .strategies import job_infos


def write(tmp_path: Path, body: str, name: str = "job.sh") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def test_parse_pbs_directives_keys_by_letter_and_joins_l(tmp_path: Path) -> None:
    """`#PBS -<x>` keys by letter; repeated `-l` lines join with newlines; non-flags drop."""
    script = write(
        tmp_path,
        "#!/bin/bash\n"
        "#PBS -N myjob\n"
        "#PBS -q gen-S\n"
        "#PBS -l select=2:ncpus=4\n"
        "#PBS -l walltime=01:00:00\n"
        "#PBS notaflag here\n"
        "echo hi\n",
    )
    directives = _parse_pbs_directives(script)
    assert directives["N"] == "myjob"
    assert directives["q"] == "gen-S"
    assert directives["l"] == "select=2:ncpus=4\nwalltime=01:00:00"
    assert "notaflag" not in directives


def test_parse_sbatch_directives_normalises_short_to_long(tmp_path: Path) -> None:
    """Short `-t`/`-p`/`-J` map to long names; `--k=v` parses; last occurrence wins."""
    script = write(
        tmp_path,
        "#!/bin/bash\n"
        "#SBATCH -t 02:00:00\n"
        "#SBATCH -p gpu\n"
        "#SBATCH -J first\n"
        "#SBATCH --job-name=second\n"
        "#SBATCH --gpus=4\n"
        "#SBATCH bareword\n"  # neither -- nor - prefixed: skipped, not a directive
        "run\n",
    )
    directives = _parse_sbatch_directives(script)
    assert directives["time"] == "02:00:00"
    assert directives["partition"] == "gpu"
    assert directives["job-name"] == "second"  # long form, last writer wins
    assert directives["gpus"] == "4"
    assert "bareword" not in directives


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("32G", 32),
        ("gpu:2", None),
        ("  16gb ", 16),
        ("", None),
        (None, None),
        ("0", 0),
    ],
)
def test_int_or_none_takes_leading_integer(value: str | None, expected: int | None) -> None:
    """Only a leading run of digits parses; anything else (or None/empty) is None."""
    assert _int_or_none(value) == expected


@given(st.integers(min_value=0, max_value=10_000))
def test_int_or_none_roundtrips_pure_integers(number: int) -> None:
    """A bare integer string always parses back to itself."""
    assert _int_or_none(str(number)) == number


def test_resolve_script_returns_existing_file(tmp_path: Path) -> None:
    """An absolute/relative path to a real file resolves to itself."""
    script = write(tmp_path, "echo hi\n")
    assert _resolve_script(str(script)) == script


def test_resolve_script_globs_experiments_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare name resolves against `experiments/*/jobs/<name>*.sh` under the CWD."""
    jobs = tmp_path / "experiments" / "exp1" / "jobs"
    jobs.mkdir(parents=True)
    target = jobs / "sampler_ablation.sh"
    target.write_text("run\n")
    monkeypatch.chdir(tmp_path)
    assert _resolve_script("sampler_ablation") == target


def test_resolve_script_prefers_research_root_over_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A match under the research projects tree resolves first, never touching the CWD glob."""
    import lote.executor.cli as exec_cli

    jobs = tmp_path / "projects" / "tb" / "experiments" / "exp" / "jobs"
    jobs.mkdir(parents=True)
    target = jobs / "train.sh"
    target.write_text("run\n")
    monkeypatch.setattr(exec_cli, "experiments_root", lambda: tmp_path)
    assert _resolve_script("train") == target


def test_resolve_script_ambiguous_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same script stem in two experiments is an error, not a silent pick."""
    for exp in ("exp1", "exp2"):
        jobs = tmp_path / "experiments" / exp / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "train.sh").write_text("x\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="ambiguous"):
        _resolve_script("train")


def test_resolve_script_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No match anywhere raises a clear FileNotFoundError."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="no .sh found"):
        _resolve_script("nope")


@given(jobs=st.lists(job_infos(), min_size=1, max_size=6))
def test_resolve_jid_or_name_full_id_always_matches(jobs: list[object]) -> None:
    """Every job's full id resolves to (at least) itself, regardless of name collisions."""
    from lote.clients.pbs import JobInfo

    typed: list[JobInfo] = jobs  # type: ignore[assignment]
    for job in typed:
        assert job.job_id in _resolve_jid_or_name(job.job_id, typed)


def test_resolve_jid_or_name_matches_prefix_and_short_id() -> None:
    """A truncated name prefix and the `.`-stripped short id both match (PBS truncation)."""
    from lote.clients.pbs import JobInfo, JobState

    jobs = [
        JobInfo(
            job_id="123.server",
            name="sampler_ablation",
            user="u",
            state=JobState.RUNNING,
            queue="q",
        ),
        JobInfo(job_id="456.server", name="other", user="u", state=JobState.QUEUED, queue="q"),
    ]
    assert _resolve_jid_or_name("123", jobs) == ["123.server"]  # short id
    assert _resolve_jid_or_name("sampler_ab", jobs) == ["123.server"]  # truncated prefix
    assert _resolve_jid_or_name("nomatch", jobs) == []
