from lote.jobspec import JobSpec


def render(*, pbs: bool, gpu_in_select: bool = True, **overrides: str | int) -> str:
    """A rendered job script for `python -m foo` with the given JobSpec overrides."""
    return JobSpec(cmd="python -m foo", **overrides).render(pbs=pbs, gpu_in_select=gpu_in_select)


def test_job_sources_chefe_activate_when_present() -> None:
    """The generated body sources `.chefe/activate.sh` (modules + pixi env) and runs the cmd."""
    text = render(pbs=False)
    assert "if [ -f .chefe/activate.sh ]; then" in text
    assert "source .chefe/activate.sh" in text
    assert "python -m foo" in text


def test_job_clears_inherited_pythonpath_by_default() -> None:
    """A general remote job cannot inherit an unrelated monorepo package search path."""
    text = render(pbs=False)
    assert "unset PYTHONPATH" in text
    assert "bash -c 'python -m foo'" in text


def test_activate_branch_honours_a_custom_pythonpath() -> None:
    """An explicit search path is exported once before either execution branch."""
    text = render(pbs=False, pythonpath="src:libs")
    assert "export PYTHONPATH=src:libs" in text


def test_pbs_activate_branch_applies_pythonpath() -> None:
    """PBS jobs clear inherited search paths just like local queue jobs."""
    text = render(pbs=True)
    assert "unset PYTHONPATH" in text
    assert "bash -c 'python -m foo'" in text


def test_job_falls_back_to_chefe_run_when_activate_absent() -> None:
    """With no activation or built env the body runs the command through Chefe."""
    text = render(pbs=False)
    assert "CHEFE_FALLBACK=1" in text
    assert "chefe run bash -c 'python -m foo'" in text


def test_job_runs_pixi_env_python_directly_when_only_env_present() -> None:
    """With no activate.sh but a built env, the body runs the pixi env's python directly.

    A host whose `chefe` is missing or out of date still runs -- no `chefe` is invoked at job time.
    """
    text = render(pbs=False)
    assert 'elif [ -x ".chefe/.pixi/envs/default/bin/python" ]; then' in text
    assert 'export PATH="$PWD/.chefe/.pixi/envs/default/bin:$PATH"' in text


def test_render_pbs_job_assembles_header_guard_and_body() -> None:
    """A PBS job carries its header, guard, synchronous log redirect, then the run body."""
    text = render(pbs=True, queue="debug-g", walltime="00:30:00", gpus=1)
    assert text.startswith("#!/bin/bash\n")
    assert "#PBS -q debug-g" in text
    assert "#PBS -l select=1:ngpus=1" in text
    assert "#PBS -l walltime=00:30:00" in text
    assert "#PBS -j oe" in text  # merged output, so `lote logs` finds it
    assert "set -euo pipefail" in text
    assert 'cd "${PBS_O_WORKDIR:-$PWD}"' in text
    assert 'lote_log=".lote/logs/${PBS_JOBID%%.*}.log"' in text
    assert 'exec >> "$lote_log" 2>&1' in text
    assert "tee" not in text
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
    """A custom search path is exported for every PBS execution branch."""
    text = render(pbs=True, pythonpath="src")
    assert "export PYTHONPATH=src" in text


def test_render_bash_job_drops_pbs_header_but_keeps_body() -> None:
    """A bash wrapper has no #PBS header; the activate.sh-sourcing body stays."""
    text = render(pbs=False)
    assert "#PBS" not in text
    assert "set -euo pipefail" in text
    assert "source .chefe/activate.sh" in text
    assert "unset PYTHONPATH" in text


def test_compound_command_runs_in_one_shell() -> None:
    """Environment setup covers every statement in a compound remote command."""
    text = JobSpec(cmd="set -a; echo ready").render(pbs=False)
    assert "bash -c 'set -a; echo ready'" in text
