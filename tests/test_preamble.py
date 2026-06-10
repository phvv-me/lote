from __future__ import annotations

from pathlib import Path

from lote.executor.preamble import write_wrapper


def test_wrapper_injects_cd_path_and_execs_script(tmp_path: Path) -> None:
    """The wrapper cds into the submit dir, puts user bins on PATH, then execs the script."""
    script = tmp_path / "job.sh"
    script.write_text("echo hi\n")
    logs = tmp_path / "logs"
    logs.mkdir()

    wrapper = write_wrapper(script, logs, workdir_var="PBS_O_WORKDIR")
    text = wrapper.read_text()

    assert wrapper.name == "job.wrapper.sh"
    assert 'cd "${PBS_O_WORKDIR:-$HOME}"' in text  # rescues a cd-less job from $HOME
    assert "export PATH=$HOME/.local/bin" in text  # makes chefe findable
    assert f"exec bash {script.resolve()}" in text  # runs the user script unchanged


def test_wrapper_dry_run_returns_path_without_writing(tmp_path: Path) -> None:
    """dry_run yields the wrapper path but creates no file (no side effects)."""
    script = tmp_path / "job.sh"
    script.write_text("x\n")
    logs = tmp_path / "logs"
    logs.mkdir()

    wrapper = write_wrapper(script, logs, workdir_var="SLURM_SUBMIT_DIR", dry_run=True)

    assert wrapper.name == "job.wrapper.sh"
    assert not wrapper.exists()
