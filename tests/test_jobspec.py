from lote.jobspec import DEFAULT_PYTHONPATH, JobSpec


def render(*, pbs: bool, gpu_in_select: bool = True, **overrides: str | int) -> str:
    """A rendered job script for `python -m foo` with the given JobSpec overrides."""
    return JobSpec(cmd="python -m foo", **overrides).render(pbs=pbs, gpu_in_select=gpu_in_select)


def test_job_sources_chefe_activate_when_present() -> None:
    """The generated body sources `.chefe/activate.sh` (modules + pixi env) and runs the cmd."""
    text = render(pbs=False)
    assert "if [ -f .chefe/activate.sh ]; then" in text
    assert "source .chefe/activate.sh" in text
    assert "python -m foo" in text


def test_job_applies_pythonpath_in_the_activate_branch() -> None:
    """The activate.sh branch runs the cmd under PYTHONPATH too, not only the fallbacks.

    Regression for the silent drop: an onboarded host has `.chefe/activate.sh`, so the primary
    branch ran. It used to exec a bare `{{ cmd }}` with no PYTHONPATH, so an experiment importing
    a repo-relative package (the common case) died with `ModuleNotFoundError` while the two
    activate.sh-absent fallbacks worked. The branch now prepends the requested PYTHONPATH ahead of
    whatever activate.sh exported, so a relative import resolves on every host.
    """
    text = render(pbs=False)
    activate_branch = text.split("source .chefe/activate.sh", 1)[1].split("elif", 1)[0]
    assert f"PYTHONPATH={DEFAULT_PYTHONPATH}" in activate_branch
    assert "python -m foo" in activate_branch
    # activate.sh's own PYTHONPATH is preserved as a suffix, never clobbered.
    assert "${PYTHONPATH:+:$PYTHONPATH}" in activate_branch


def test_activate_branch_honours_a_custom_pythonpath() -> None:
    """A non-default --pythonpath reaches the activate.sh branch, not just the fallbacks."""
    activate_branch = (
        render(pbs=False, pythonpath="src:libs")
        .split("source .chefe/activate.sh", 1)[1]
        .split("elif", 1)[0]
    )
    assert "PYTHONPATH=src:libs" in activate_branch


def test_pbs_activate_branch_applies_pythonpath() -> None:
    """The PBS script's activate.sh branch carries PYTHONPATH too (HPC runs it, not a fallback)."""
    activate_branch = (
        render(pbs=True).split("source .chefe/activate.sh", 1)[1].split("elif", 1)[0]
    )
    assert f"PYTHONPATH={DEFAULT_PYTHONPATH}" in activate_branch
    assert "python -m foo" in activate_branch


def test_job_falls_back_to_chefe_run_when_activate_absent() -> None:
    """With no activate.sh the body runs a bare `chefe run env PYTHONPATH=... <cmd>`."""
    text = render(pbs=False)
    assert "else" in text
    assert f"chefe run env PYTHONPATH={DEFAULT_PYTHONPATH} python -m foo" in text


def test_job_runs_pixi_env_python_directly_when_only_env_present() -> None:
    """With no activate.sh but a built env, the body runs the pixi env's python directly.

    A host whose `chefe` is missing or out of date still runs -- no `chefe` is invoked at job time.
    """
    text = render(pbs=False)
    assert 'elif [ -x ".chefe/.pixi/envs/default/bin/python" ]; then' in text
    assert (
        f'PATH="$PWD/.chefe/.pixi/envs/default/bin:$PATH" '
        f"PYTHONPATH={DEFAULT_PYTHONPATH} python -m foo" in text
    )


def test_render_pbs_job_assembles_header_guard_and_body() -> None:
    """A PBS job carries the #PBS header, the cd/strict guard, the tee, then the run body."""
    text = render(pbs=True, queue="debug-g", walltime="00:30:00", gpus=1)
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
    text = render(pbs=True, select=2, gpus=0)
    assert "#PBS -l select=2\n" in text
    assert "ngpus" not in text


def test_bash_job_self_imposes_a_timeout_from_walltime() -> None:
    """A pueue/bash host has no scheduler, so the wrapper re-execs under its `timeout` budget.

    The re-exec runs the script through `bash`, not as a bare executable: pueue invokes it as
    `bash job.sh` so the file has no +x bit, and `timeout <script>` would die `Permission denied`.
    """
    text = render(pbs=False, walltime="02:00:00")
    assert 'timeout --kill-after=30s 7200 bash "$0"' in text
    assert "LOTE_TIMED" in text  # guard so the re-exec happens once


def test_pbs_job_uses_native_walltime_not_a_timeout_wrapper() -> None:
    """PBS enforces walltime itself, so its script has no `timeout` re-exec."""
    text = render(pbs=True, walltime="02:00:00")
    assert "#PBS -l walltime=02:00:00" in text
    assert "timeout" not in text


def test_render_pbs_job_omits_ngpus_when_queue_provides_gpu() -> None:
    """A host that hands GPUs out with the queue (Miyabi) renders a bare select, never ngpus."""
    text = render(pbs=True, gpus=1, gpu_in_select=False)
    assert "#PBS -l select=1\n" in text
    assert "ngpus" not in text


def test_render_pbs_job_requests_memory_in_the_select_chunk() -> None:
    """`--mem` joins the select chunk as `mem=NNgb`, so a hungry job gets the headroom it asks for.

    The 14B/32B jobs were OOM-killed (exit 137) under the default memory grant; requesting the
    memory explicitly is what keeps the scheduler from packing the node too tight.
    """
    text = render(pbs=True, gpus=1, mem_gb=240)
    assert "#PBS -l select=1:ngpus=1:mem=240gb" in text


def test_render_pbs_job_omits_memory_when_unset() -> None:
    """No `--mem` leaves the select chunk free of a `mem=` clause (scheduler default applies)."""
    assert "mem=" not in render(pbs=True, gpus=1)


def test_render_pbs_job_emits_group_list_only_when_account_set() -> None:
    """An account renders as `#PBS -W group_list=`; the default omits the directive."""
    assert "#PBS -W group_list=xg25g007" in render(pbs=True, account="xg25g007")
    assert "group_list" not in render(pbs=True)


def test_render_pbs_job_honours_custom_pythonpath() -> None:
    """A custom --pythonpath flows into the activate.sh-absent fallback."""
    text = render(pbs=True, pythonpath="src")
    assert "chefe run env PYTHONPATH=src python -m foo" in text


def test_render_bash_job_drops_pbs_header_but_keeps_body() -> None:
    """A bash wrapper has no #PBS header; the activate.sh-sourcing body stays."""
    text = render(pbs=False)
    assert "#PBS" not in text
    assert "set -euo pipefail" in text
    assert "source .chefe/activate.sh" in text
    assert f"chefe run env PYTHONPATH={DEFAULT_PYTHONPATH} python -m foo" in text
