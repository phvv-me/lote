"""End-to-end checks of the `--cmd` path: the exact generated job-script text and the exact
scheduler argv, for a pueue host and a PBS host both, mocking only the ssh/process seam.

The bug class these guard against is a flag that renders into a `JobSpec` but never reaches the
shell the experiment actually runs in. The fragment-level tests in `test_jobspec.py` assert the
template emits a string; these assert the *whole* script a real host would source, then hand that
exact script to a scheduler double and assert the *exact* argv it builds. So a non-default
`--pythonpath` driving a relative-import experiment is verified to land on PATH on both backends.
"""

import os
import subprocess
from pathlib import Path

import pytest

from lote.jobspec import JobSpec
from lote.schedulers import Pbs, Pueue, Resources

from .conftest import RecordingMachine

# A research entry point that only resolves when PYTHONPATH carries the repo-relative src tree --
# the relative-import experiment the prompt asks the generated script to be able to run.
EXPERIMENT = "python -m projects.compression.run --model X"
PYTHONPATH = "research:research/projects/compression/src"


def test_pueue_cmd_job_renders_exact_bash_script_running_the_experiment() -> None:
    """A pueue host's `--cmd` job is the exact bash wrapper, with PYTHONPATH on every branch.

    The wrapper has no scheduler header, self-imposes the walltime budget as a `timeout` re-exec,
    and -- the regression -- runs the experiment under the requested PYTHONPATH in the activate.sh
    branch (the one an onboarded host takes), so a repo-relative `python -m projects...` resolves.
    """
    script = JobSpec(cmd=EXPERIMENT, walltime="02:00:00", pythonpath=PYTHONPATH).render(pbs=False)
    assert script == (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "# The caller chose a walltime and this host has no scheduler to enforce it, so re-run"
        " under\n"
        "# `timeout` of the same budget once (guarded by LOTE_TIMED) and stamp a clear verdict"
        " into the\n"
        "# captured log when the budget kills the job, so `lote why` decodes the stop instead of"
        " showing\n"
        "# a raw SIGTERM backtrace. Without an explicit walltime the job runs uncapped.\n"
        'if [ -z "${LOTE_TIMED:-}" ]; then\n'
        "  export LOTE_TIMED=1\n"
        "  status=0\n"
        '  timeout --kill-after=30s 7200 bash "$0" "$@" || status=$?\n'
        '  if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then\n'
        '    echo "lote: killed at walltime 02:00:00 (exit $status)"\n'
        "  fi\n"
        '  exit "$status"\n'
        "fi\n"
        "if [ -f .chefe/activate.sh ]; then\n"
        "  source .chefe/activate.sh\n"
        'elif [ -x ".chefe/.pixi/envs/default/bin/python" ]; then\n'
        '  export PATH="$PWD/.chefe/.pixi/envs/default/bin:$PATH"\n'
        "else\n"
        "  CHEFE_FALLBACK=1\n"
        "fi\n"
        f"export PYTHONPATH={PYTHONPATH}\n"
        'if [ -n "${CHEFE_FALLBACK:-}" ]; then\n'
        f"  chefe run bash -c '{EXPERIMENT}'\n"
        "else\n"
        f"  bash -c '{EXPERIMENT}'\n"
        "fi\n"
    )


def test_pbs_cmd_job_renders_exact_pbs_script_running_the_experiment() -> None:
    """A PBS host's `--cmd` job is the exact #PBS script, with PYTHONPATH on every branch.

    PBS enforces walltime itself (no `timeout` re-exec), redirects merged output where `lote logs`
    finds it, and the activate.sh branch -- the one a compute node actually runs -- carries the
    requested PYTHONPATH so the same relative-import experiment resolves on the cluster.
    """
    script = JobSpec(
        cmd=EXPERIMENT,
        queue="debug-g",
        walltime="00:30:00",
        gpus=1,
        account="xg25g007",
        pythonpath=PYTHONPATH,
    ).render(pbs=True)
    assert script == (
        "#!/bin/bash\n"
        "#PBS -q debug-g\n"
        "#PBS -l select=1:ngpus=1\n"
        "#PBS -l walltime=00:30:00\n"
        "#PBS -W group_list=xg25g007\n"
        "#PBS -j oe\n"
        "set -euo pipefail\n"
        'cd "${PBS_O_WORKDIR:-$PWD}"\n'
        "mkdir -p .lote/logs\n"
        "# Record the job's own exit into an artifact the laptop can autopsy after the PBS server"
        " purges\n"
        "# the job from qstat/history; the TERM trap turns the walltime SIGTERM into exit 143 so"
        " the EXIT\n"
        "# trap still runs (a SIGKILL leaves no artifact, which reads as vanished). Append the"
        " same stamp\n"
        "# to the captured log first, after every command write has completed.\n"
        'lote_log=".lote/logs/${PBS_JOBID%%.*}.log"\n'
        "lote_exit() {\n"
        '  local status="$1"\n'
        '  printf \'exit=%s\\n\' "$status" >> "$lote_log"\n'
        '  printf \'exit=%s\\n\' "$status" > ".lote/logs/${PBS_JOBID%%.*}.exit"\n'
        "}\n"
        "trap 'lote_exit $?' EXIT\n"
        "trap 'exit 143' TERM\n"
        'exec >> "$lote_log" 2>&1\n'
        "if [ -f .chefe/activate.sh ]; then\n"
        "  source .chefe/activate.sh\n"
        'elif [ -x ".chefe/.pixi/envs/default/bin/python" ]; then\n'
        '  export PATH="$PWD/.chefe/.pixi/envs/default/bin:$PATH"\n'
        "else\n"
        "  CHEFE_FALLBACK=1\n"
        "fi\n"
        f"export PYTHONPATH={PYTHONPATH}\n"
        'if [ -n "${CHEFE_FALLBACK:-}" ]; then\n'
        f"  chefe run bash -c '{EXPERIMENT}'\n"
        "else\n"
        f"  bash -c '{EXPERIMENT}'\n"
        "fi\n"
    )


def test_pbs_wrapper_captures_instant_failure_before_exit_stamp(tmp_path: Path) -> None:
    """An immediate failure preserves both output streams before its final status stamp."""
    activate = tmp_path / ".chefe" / "activate.sh"
    activate.parent.mkdir()
    activate.write_text(":\n")
    wrapper = tmp_path / "job.sh"
    wrapper.write_text(
        JobSpec(
            cmd=(
                "printf 'stdout-before-failure\\n'; printf 'stderr-before-failure\\n' >&2; exit 86"
            )
        ).render(pbs=True)
    )

    result = subprocess.run(
        ["bash", str(wrapper)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PBS_JOBID": "2441153.miyabi",
            "PBS_O_WORKDIR": str(tmp_path),
        },
        check=False,
        timeout=5,
    )

    log = tmp_path / ".lote" / "logs" / "2441153.log"
    artifact = tmp_path / ".lote" / "logs" / "2441153.exit"
    assert result.returncode == 86
    assert log.read_text().splitlines() == [
        "stdout-before-failure",
        "stderr-before-failure",
        "exit=86",
    ]
    assert artifact.read_text() == "exit=86\n"


def test_pueue_submit_builds_exact_argv_for_a_generated_script(remote: RecordingMachine) -> None:
    """The pueue backend enqueues the generated script's path under the daemon's working dir.

    The exact argv pueue runs: `pueue add --print-task-id --label <stem> --working-directory <root>
    -- <activated `lote exec run <script>`>`. The activation prefix puts the user bins on PATH so
    `chefe` resolves, and pueue owns the cwd (so no `cd`), exactly as a real dispatch does.
    """
    remote.outputs = ["42\n"]
    script = ".lote/jobs/job-abc123.sh"
    handle = Pueue().submit(remote, "/work/repo", script, [], resources=Resources())
    assert handle == "42"
    [argv] = remote.calls
    assert argv == [
        "pueue", "add", "--print-task-id",
        "--label", "job-abc123",
        "--working-directory", "/work/repo",
        "--",
        "export PATH=$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin:$PATH && "
        f"chefe run lote exec run {script}",
    ]  # fmt: skip


def test_pbs_submit_builds_exact_login_shell_argv_for_a_generated_script(
    remote: RecordingMachine,
) -> None:
    """The PBS backend runs the generated script through `lote exec qsub` in a login shell.

    The exact argv: `bash -lc 'cd <root> && export PATH=<bins>:$PATH && chefe run lote exec qsub
    <script>'`. The login shell sources the cluster toolchain so `qsub` is on PATH, and the handle
    is qsub's job id (its stdout's last line).
    """
    remote.outputs = ["98765.pbs1\n"]
    script = ".lote/jobs/job-abc123.sh"
    handle = Pbs().submit(remote, "/work/repo", script, [], resources=Resources())
    assert handle == "98765.pbs1"
    [argv] = remote.calls
    assert argv == [
        "bash", "-lc",
        "cd /work/repo && "
        "export PATH=$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin:$PATH && "
        f"chefe run lote exec qsub {script}",
    ]  # fmt: skip


@pytest.mark.parametrize("pbs", [False, True])
def test_generated_script_resolves_a_relative_import_under_the_chosen_pythonpath(
    pbs: bool, tmp_path: Path
) -> None:
    """The activate.sh branch the host runs assigns the requested PYTHONPATH before the experiment.

    This is the property a relative import depends on: on the line that runs `python -m
    projects...`, `PYTHONPATH=` must already carry the repo-relative src tree. Asserted for both
    the bash wrapper (pueue) and the #PBS script (cluster), since both share the body.
    """
    script = JobSpec(cmd=EXPERIMENT, pythonpath=PYTHONPATH).render(pbs=pbs)
    assert f"export PYTHONPATH={PYTHONPATH}" in script
    assert f"bash -c '{EXPERIMENT}'" in script
