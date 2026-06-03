from __future__ import annotations

import pytest
from hypothesis import given

from fleet.clients.slurm import SlurmState
from fleet.models import Target
from fleet.reconcile import parse_pbs_record, pbs_verdict, pueue_verdict
from fleet.schedulers import (
    JobState,
    Local,
    Pbs,
    Pueue,
    Resources,
    Slurm,
    build_sbatch_flags,
    pick,
    slurm_verdict,
)
from fleet.schedulers._remote import remote_exec

from .strategies import pueue_tasks, resources

# --- pick() kind -> class ---


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("pbs", Pbs), ("slurm", Slurm), ("ssh", Pueue), ("unknown", Pueue)],
)
def test_pick_maps_kind_to_scheduler(kind: str, expected: type) -> None:
    """The probed `kind` selects its backend; anything unknown falls back to pueue."""
    assert isinstance(pick(Target(name="h", kind=kind)), expected)


# --- remote_exec login-shell wrapper ---


def test_remote_exec_builds_login_shell_string() -> None:
    """`remote_exec` cds into the root then runs `chefe run fleet exec`, shell-quoting args."""
    assert remote_exec("/repo root", "qsub", "x.sh", "--gpus=2") == (
        "cd '/repo root' && chefe run fleet exec qsub x.sh --gpus=2"
    )


# --- Resources -> sbatch flags ---


def test_build_sbatch_flags_only_set_fields() -> None:
    """gpus is always emitted; the rest appear only when set."""
    assert build_sbatch_flags(Resources(gpus=4)) == ["--gpus=4"]
    flags = build_sbatch_flags(
        Resources(gpus=2, walltime="01:00:00", queue="gpu", account="proj", mem_gb=32)
    )
    assert flags == [
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


# --- verdict mapping ---


@pytest.mark.parametrize(
    ("state", "exit_code", "verdict"),
    [
        (None, None, "vanished"),
        ("R", None, "running"),
        ("Q", None, "running"),
        ("F", 0, "ok"),
        ("F", 1, "failed"),
        ("E", None, "ok"),  # finished, no exit status reported
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
def test_slurm_verdict(state: object, exit_code: int | None, verdict: str) -> None:
    """SLURM verdict mirrors PBS over its long state names and exit code."""
    assert slurm_verdict(state, exit_code) == verdict  # type: ignore[arg-type]


@given(pueue_tasks())
def test_pueue_verdict_total(task: object) -> None:
    """pueue verdict is one of the four words for any task, and vanished for None."""
    from fleet.clients.pueue.task import PueueTask

    typed: PueueTask = task  # type: ignore[assignment]
    assert pueue_verdict(typed) in {"ok", "failed", "running", "vanished"}
    assert pueue_verdict(None) == "vanished"


def test_parse_pbs_record_extracts_state_and_exit() -> None:
    """A qstat -f record yields its job_state and Exit_status."""
    record = "Job Id: 1.s\n    job_state = F\n    Exit_status = 0\n"
    assert parse_pbs_record(record) == ("F", 0)


def test_parse_pbs_record_vanished() -> None:
    """An empty/irrelevant record means the job is gone from history."""
    assert parse_pbs_record("qstat: Unknown Job Id\n") == (None, None)


# --- command construction via the recording remote ---


def test_pbs_submit_returns_last_line_of_login_shell(remote) -> None:  # noqa: ANN001
    """Pbs.submit runs `bash -lc <remote_exec qsub ...>` and returns the trailing line (the id)."""
    remote.outputs = ["info...\n123.pbs\n"]
    handle = Pbs().submit(remote, "/repo", "x.sh", ["--n", "1"], resources=Resources())
    assert handle == "123.pbs"
    [call] = remote.calls
    assert call[0] == "bash" and call[1] == "-lc"
    assert "chefe run fleet exec qsub x.sh --n 1" in call[2]


def test_slurm_submit_threads_resource_flags(remote) -> None:  # noqa: ANN001
    """Slurm.submit folds Resources into `sbatch` override flags inside the login-shell string."""
    remote.outputs = ["Submitted batch job 42\n42\n"]
    handle = Slurm().submit(remote, "/repo", "x.sh", [], resources=Resources(gpus=2, queue="gpu"))
    assert handle == "42"
    assert "exec sbatch x.sh --gpus=2 --partition=gpu" in remote.calls[0][2]


def test_pbs_state_parses_record_into_jobstate(remote) -> None:  # noqa: ANN001
    """Pbs.state runs `info <handle>` and folds the record into a JobState with a verdict."""
    remote.outputs = ["Job Id: 7.s\n    job_state = F\n    Exit_status = 0\n"]
    state = Pbs().state(remote, "/repo", "7.s")
    assert isinstance(state, JobState)
    assert state.state == "F" and state.exit_code == 0 and state.verdict == "ok"
    assert "info 7.s" in remote.calls[0][2]


def test_slurm_state_runs_sacct_directly(remote) -> None:  # noqa: ANN001
    """Slurm.state runs the bare `sacct` builder (not the login shell) and parses its output."""
    remote.outputs = ["7|COMPLETED|0:0\n"]
    state = Slurm().state(remote, "/repo", "7")
    assert state.state == "COMPLETED" and state.verdict == "ok"
    assert remote.calls[0][0] == "sacct"


def test_pueue_submit_enqueues_exec_run(remote) -> None:  # noqa: ANN001
    """Pueue.submit enqueues `chefe run fleet exec run <script>` and returns pueue's task id."""
    remote.outputs = ["17\n"]
    handle = Pueue().submit(remote, "/repo", "train.sh", ["--n", "1"], resources=Resources())
    assert handle == "17"
    [call] = remote.calls
    assert call[0] == "pueue" and call[1] == "add"
    assert call[-1] == "chefe run fleet exec run train.sh --n 1"
    assert "train" in call  # label is the script stem


def test_pueue_state_resolves_handle_from_status(remote) -> None:  # noqa: ANN001
    """Pueue.state finds the task by id in one `pueue status --json` snapshot."""
    import json

    snapshot = {
        "tasks": {"0": {"id": 17, "label": "train", "status": {"Done": {"result": "Success"}}}}
    }
    remote.outputs = [json.dumps(snapshot)]
    state = Pueue().state(remote, "/repo", "17")
    assert state.state == "Done" and state.exit_code == 0 and state.verdict == "ok"


def test_local_submit_runs_and_state_vanishes(remote) -> None:  # noqa: ANN001
    """Local.submit runs `run <script>` via FG and returns the script; state is always vanished."""
    handle = Local().submit(remote, "/repo", "x.sh", [], resources=Resources())
    assert handle == "x.sh"
    assert "exec run x.sh" in remote.calls[0][2]
    assert Local().state(remote, "/repo", "x.sh").verdict == "vanished"
