from __future__ import annotations

from lote.jobspec import DEFAULT_PYTHONPATH, render_bash_job, render_pbs_job


def test_job_sources_chefe_activate_when_present() -> None:
    """The generated body sources `.chefe/activate.sh` (modules + pixi env) and runs the cmd."""
    text = render_bash_job("python -m foo")
    assert "if [ -f .chefe/activate.sh ]; then" in text
    assert "source .chefe/activate.sh" in text
    assert "python -m foo" in text


def test_job_falls_back_to_chefe_run_when_activate_absent() -> None:
    """With no activate.sh the body runs a bare `chefe run env PYTHONPATH=... <cmd>`."""
    text = render_bash_job("python -m foo")
    assert "else" in text
    assert f"chefe run env PYTHONPATH={DEFAULT_PYTHONPATH} python -m foo" in text


def test_job_runs_pixi_env_python_directly_when_only_env_present() -> None:
    """With no activate.sh but a built env, the body runs the pixi env's python directly.

    A host whose `chefe` is missing or out of date still runs -- no `chefe` is invoked at job time.
    """
    text = render_bash_job("python -m foo")
    assert 'elif [ -x ".chefe/.pixi/envs/default/bin/python" ]; then' in text
    assert (
        f'PATH="$PWD/.chefe/.pixi/envs/default/bin:$PATH" '
        f"PYTHONPATH={DEFAULT_PYTHONPATH} python -m foo" in text
    )


def test_no_inline_module_preamble() -> None:
    """The inline module discovery + libstdc++ preload is gone; activate.sh owns the env now."""
    text = render_pbs_job("python -m foo")
    assert "module -t avail" not in text
    assert "LD_PRELOAD" not in text
    assert "libstdc++" not in text


def test_render_pbs_job_assembles_header_guard_and_body() -> None:
    """A PBS job carries the #PBS header, the cd/strict guard, the tee, then the run body."""
    text = render_pbs_job("python -m foo", queue="debug-g", walltime="00:30:00", gpus=1)
    assert text.startswith("#!/bin/bash\n")
    assert "#PBS -q debug-g" in text
    assert "#PBS -l select=1:ngpus=1" in text
    assert "#PBS -l walltime=00:30:00" in text
    assert "#PBS -j oe" in text  # merged output, so `lote logs` finds it
    assert "set -euo pipefail" in text
    assert 'cd "${PBS_O_WORKDIR:-$PWD}"' in text
    assert 'exec > >(tee ".lote/logs/${PBS_JOBID%%.*}.log") 2>&1' in text
    assert "source .chefe/activate.sh" in text


def test_render_pbs_job_omits_ngpus_when_zero() -> None:
    """gpus=0 requests bare nodes (no `:ngpus=` suffix)."""
    text = render_pbs_job("python -m foo", select=2, gpus=0)
    assert "#PBS -l select=2\n" in text
    assert "ngpus" not in text


def test_render_pbs_job_honours_custom_pythonpath() -> None:
    """A custom --pythonpath flows into the activate.sh-absent fallback."""
    text = render_pbs_job("python -m foo", pythonpath="src")
    assert "chefe run env PYTHONPATH=src python -m foo" in text


def test_render_bash_job_drops_pbs_header_but_keeps_body() -> None:
    """A bash wrapper has no #PBS header; the activate.sh-sourcing body stays."""
    text = render_bash_job("python -m foo")
    assert "#PBS" not in text
    assert "set -euo pipefail" in text
    assert "source .chefe/activate.sh" in text
    assert f"chefe run env PYTHONPATH={DEFAULT_PYTHONPATH} python -m foo" in text
