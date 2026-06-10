from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lote.clients.pueue.task import PueueTask
from lote.clients.slurm import SlurmState
from lote.environment import Environment
from lote.models import Target
from lote.reconcile import parse_pbs_record, pbs_verdict, pueue_verdict
from lote.schedulers import (
    JobState,
    Local,
    Pbs,
    Pueue,
    Resources,
    Scheduler,
    Slurm,
    build_sbatch_flags,
    pick,
    poll_until_done,
    slurm_verdict,
)

from .conftest import RecordingMachine
from .strategies import pueue_tasks, resources


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("pbs", Pbs), ("slurm", Slurm), ("ssh", Pueue), ("unknown", Pueue)],
)
def test_pick_maps_kind_to_scheduler(kind: str, expected: type) -> None:
    """The probed `kind` selects its backend; anything unknown falls back to pueue."""
    assert isinstance(pick(Target(name="h", kind=kind)), expected)


@given(st.text(min_size=0, max_size=8))
def test_pick_always_returns_a_scheduler(kind: str) -> None:
    """`pick` resolves a `Scheduler` for any kind string, defaulting to pueue."""
    assert isinstance(pick(Target(name="h", kind=kind)), Scheduler)


def test_exec_command_builds_login_shell_string() -> None:
    """`exec_command` cds into the root, exports the user bins, then runs `chefe run`."""
    bins = "$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin"
    assert Environment(root="/repo root").exec_command("qsub", "x.sh", "--gpus=2") == (
        f"cd '/repo root' && export PATH={bins}:$PATH && chefe run lote exec qsub x.sh --gpus=2"
    )


def test_build_sbatch_flags_only_set_fields() -> None:
    """gpus is always emitted; the rest appear only when set, in resource order."""
    assert build_sbatch_flags(Resources(gpus=4)) == ["--gpus=4"]
    assert build_sbatch_flags(
        Resources(gpus=2, walltime="01:00:00", queue="gpu", account="proj", mem_gb=32)
    ) == [
        "--gpus=2",
        "--walltime=01:00:00",
        "--partition=gpu",
        "--account=proj",
        "--mem-gb=32",
    ]


@given(resources())
def test_build_sbatch_flags_always_starts_with_gpus(res: Resources) -> None:
    """The gpus flag always leads, and every set optional field contributes exactly one flag."""
    flags = build_sbatch_flags(res)
    assert flags[0] == f"--gpus={res.gpus}"
    optional = sum(x is not None for x in (res.walltime, res.queue, res.account, res.mem_gb))
    assert len(flags) == 1 + optional


@pytest.mark.parametrize(
    ("state", "exit_code", "verdict"),
    [
        (None, None, "vanished"),
        ("R", None, "running"),
        ("Q", None, "running"),
        ("F", 0, "ok"),
        ("F", 1, "failed"),
        ("E", None, "ok"),
    ],
)
def test_pbs_verdict(state: str | None, exit_code: int | None, verdict: str) -> None:
    """PBS verdict: gone -> vanished, non-terminal -> running, terminal -> ok/failed by code."""
    assert pbs_verdict(state, exit_code) == verdict


@pytest.mark.parametrize(
    ("state", "exit_code", "verdict"),
    [
        (None, None, "vanished"),
        (SlurmState.RUNNING, None, "running"),
        (SlurmState.PENDING, None, "running"),
        (SlurmState.COMPLETED, 0, "ok"),
        (SlurmState.COMPLETED, 1, "failed"),
        (SlurmState.FAILED, 1, "failed"),
        (SlurmState.CANCELLED, None, "failed"),
    ],
)
def test_slurm_verdict(state: SlurmState | None, exit_code: int | None, verdict: str) -> None:
    """SLURM verdict mirrors PBS over its long state names and exit code."""
    assert slurm_verdict(state, exit_code) == verdict


@given(pueue_tasks())
def test_pueue_verdict_total(task: PueueTask) -> None:
    """pueue verdict is one of the four words for any task, and vanished for None."""
    assert pueue_verdict(task) in {"ok", "failed", "running", "vanished"}
    assert pueue_verdict(None) == "vanished"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ("Job Id: 1.s\n    queue = gpu\n    job_state = F\n    Exit_status = 0\n", ("F", 0)),
        ("qstat: Unknown Job Id\n", (None, None)),
    ],
)
def test_parse_pbs_record(record: str, expected: tuple[str | None, int | None]) -> None:
    """A qstat -f record yields (job_state, Exit_status); an irrelevant record is (None, None)."""
    assert parse_pbs_record(record) == expected


def test_pbs_submit_returns_last_line_of_login_shell(remote: RecordingMachine) -> None:
    """Pbs.submit runs `bash -lc <remote_exec qsub ...>` and returns the trailing line (the id)."""
    remote.outputs = ["info...\n123.pbs\n"]
    handle = Pbs().submit(remote, "/repo", "x.sh", ["--n", "1"], resources=Resources())
    assert handle == "123.pbs"
    [call] = remote.calls
    assert call[0] == "bash" and call[1] == "-lc"
    assert "chefe run lote exec qsub x.sh --n 1" in call[2]


def test_slurm_submit_threads_resource_flags(remote: RecordingMachine) -> None:
    """Slurm.submit folds Resources into `sbatch` override flags inside the login-shell string."""
    remote.outputs = ["Submitted batch job 42\n42\n"]
    handle = Slurm().submit(remote, "/repo", "x.sh", [], resources=Resources(gpus=2, queue="gpu"))
    assert handle == "42"
    assert "exec sbatch x.sh --gpus=2 --partition=gpu" in remote.calls[0][2]


def test_pbs_state_parses_record_into_jobstate(remote: RecordingMachine) -> None:
    """Pbs.state runs `info <handle>` and folds the record into a JobState with a verdict."""
    remote.outputs = ["Job Id: 7.s\n    job_state = F\n    Exit_status = 0\n"]
    state = Pbs().state(remote, "/repo", "7.s")
    assert isinstance(state, JobState)
    assert state.state == "F" and state.exit_code == 0 and state.verdict == "ok"
    assert "info 7.s" in remote.calls[0][2]


def test_slurm_state_runs_sacct_directly(remote: RecordingMachine) -> None:
    """Slurm.state runs the bare `sacct` builder (not the login shell) and parses its output."""
    remote.outputs = ["7|COMPLETED|0:0\n"]
    state = Slurm().state(remote, "/repo", "7")
    assert state.state == "COMPLETED" and state.verdict == "ok"
    assert remote.calls[0][0] == "sacct"


def test_pueue_submit_enqueues_exec_run(remote: RecordingMachine) -> None:
    """Pueue.submit enqueues `chefe run lote exec run <script>` and returns pueue's task id."""
    remote.outputs = ["17\n"]
    handle = Pueue().submit(remote, "/repo", "train.sh", ["--n", "1"], resources=Resources())
    assert handle == "17"
    [call] = remote.calls
    assert call[0] == "pueue" and call[1] == "add"
    assert call[-1] == (
        "export PATH=$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin:$PATH && "
        "chefe run lote exec run train.sh --n 1"
    )
    assert "train" in call


def test_pueue_state_resolves_handle_from_status(remote: RecordingMachine) -> None:
    """Pueue.state finds the task by id in one `pueue status --json` snapshot."""
    snapshot = {
        "tasks": {"0": {"id": 17, "label": "train", "status": {"Done": {"result": "Success"}}}}
    }
    remote.outputs = [json.dumps(snapshot)]
    state = Pueue().state(remote, "/repo", "17")
    assert state.state == "Done" and state.exit_code == 0 and state.verdict == "ok"


def test_local_submit_runs_and_state_vanishes(remote: RecordingMachine) -> None:
    """Local.submit runs `run <script>` via FG and returns the script; state is always vanished."""
    handle = Local().submit(remote, "/repo", "x.sh", [], resources=Resources())
    assert handle == "x.sh"
    assert "exec run x.sh" in remote.calls[0][2]
    assert Local().state(remote, "/repo", "x.sh").verdict == "vanished"


# --- jobs (live listing) ---


def test_pueue_jobs_maps_tasks_to_states(remote: RecordingMachine) -> None:
    """Pueue.jobs turns each `pueue status` task into a JobState carrying its label/verdict."""
    snapshot = {
        "tasks": {"0": {"id": 5, "label": "train", "status": {"Running": {}}}},
    }
    remote.outputs = [json.dumps(snapshot)]
    [state] = Pueue().jobs(remote, "/repo")
    assert state.handle == "5" and state.label == "train" and state.verdict == "running"


def test_pbs_jobs_parses_qstat_into_states(remote: RecordingMachine) -> None:
    """Pbs.jobs runs `qstat` in a login shell and maps each row to a JobState."""
    remote.outputs = ["Job ID  Name  User  Time  S  Queue\n--\n7.s job1 u 00:01 R gpu\n"]
    [state] = Pbs().jobs(remote, "/repo")
    assert state.handle == "7.s" and state.label == "job1" and state.verdict == "running"
    assert remote.calls[0][:2] == ["bash", "-lc"] and remote.calls[0][2] == "qstat"


def test_slurm_jobs_parses_squeue_into_states(remote: RecordingMachine) -> None:
    """Slurm.jobs runs the bare `squeue` builder and maps each row to a JobState."""
    remote.outputs = ["42|job1|RUNNING|gpu|00:05\n"]
    [state] = Slurm().jobs(remote, "/repo")
    assert state.handle == "42" and state.label == "job1" and state.verdict == "running"
    assert remote.calls[0][0] == "squeue"


def test_local_jobs_is_empty(remote: RecordingMachine) -> None:
    """The queue-less Local backend lists no live jobs."""
    assert Local().jobs(remote, "/repo") == []


# --- wait (block until terminal) ---


def test_poll_until_done_loops_while_running() -> None:
    """poll_until_done polls until the verdict leaves `running`, returning the terminal state."""
    states = [
        JobState(handle="1", verdict="running"),
        JobState(handle="1", verdict="running"),
        JobState(handle="1", exit_code=0, verdict="ok"),
    ]
    slept: list[float] = []
    final = poll_until_done(lambda: states.pop(0), interval=1.0, sleeper=slept.append)
    assert final.verdict == "ok"
    assert slept == [1.0, 1.0]  # slept once per running poll, not after the terminal one


def test_pueue_wait_polls_state(remote: RecordingMachine) -> None:
    """Pueue.wait blocks on the task's state until it is terminal."""
    snapshot = {"tasks": {"0": {"id": 9, "label": "t", "status": {"Done": {"result": "Success"}}}}}
    remote.outputs = [json.dumps(snapshot)]
    final = Pueue().wait(remote, "/repo", "9")
    assert final.verdict == "ok"


def test_local_wait_returns_vanished_at_once(remote: RecordingMachine) -> None:
    """Local.wait does not poll (the job already ran in submit); it returns vanished."""
    assert Local().wait(remote, "/repo", "x.sh").verdict == "vanished"


def test_pbs_wait_blocks_on_state(remote: RecordingMachine) -> None:
    """Pbs.wait polls the job's `info` record until it is terminal."""
    remote.outputs = ["Job Id: 7.s\n    job_state = F\n    Exit_status = 0\n"]
    assert Pbs().wait(remote, "/repo", "7.s").verdict == "ok"


def test_slurm_wait_blocks_on_state(remote: RecordingMachine) -> None:
    """Slurm.wait polls `sacct` until the job is terminal."""
    remote.outputs = ["7|COMPLETED|0:0\n"]
    assert Slurm().wait(remote, "/repo", "7").verdict == "ok"
