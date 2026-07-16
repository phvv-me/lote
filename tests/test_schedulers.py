import json

import pendulum
import pytest
from hypothesis import given
from hypothesis import strategies as st
from plumbum import local
from plumbum.commands.processes import ProcessExecutionError

from lote.clients import pueue
from lote.clients.pueue.state import PueueState
from lote.clients.pueue.task import PueueTask
from lote.clients.slurm import SlurmState
from lote.environment import Environment
from lote.models import Target
from lote.reconcile import parse_pbs_record, pbs_verdict, pueue_inherited, pueue_verdict
from lote.schedulers import (
    HostUnreachable,
    JobState,
    Local,
    Pbs,
    Pueue,
    Resources,
    Scheduler,
    Slurm,
    build_sbatch_flags,
    login_run,
    pick,
    poll_until_done,
    slurm_verdict,
    stream_until_done,
)
from lote.schedulers.base import drain_log

from .conftest import RecordingMachine
from .strategies import pueue_tasks, resources


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("pbs", Pbs), ("slurm", Slurm), ("ssh", Pueue), ("local", Local)],
)
def test_pick_maps_kind_to_scheduler(kind: str, expected: type) -> None:
    """Every probed `kind` selects its backend from the module-level registry."""
    scheduler = pick(Target(name="h", kind=kind))
    assert isinstance(scheduler, expected)
    assert isinstance(scheduler, Scheduler)


@given(st.text(min_size=0, max_size=8).filter(lambda k: k not in {"pbs", "slurm", "ssh", "local"}))
def test_pick_unknown_kind_fails_fast(kind: str) -> None:
    """An unregistered kind raises a clear lookup error instead of silently dispatching."""
    with pytest.raises(LookupError, match="scheduler"):
        pick(Target(name="h", kind=kind))


def test_exec_command_builds_login_shell_string() -> None:
    """`exec_command` cds into the root, exports the user bins, then runs `chefe run`."""
    bins = "$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin"
    assert Environment(root="/repo root").exec_command("qsub", "x.sh", "--gpus=2") == (
        f"cd '/repo root' && export PATH={bins}:$PATH && chefe run lote exec qsub x.sh --gpus=2"
    )


def test_build_sbatch_flags_only_set_fields() -> None:
    """Every field appears only when set; gpus=0 (the default) is omitted entirely."""
    assert build_sbatch_flags(Resources()) == []
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
def test_build_sbatch_flags_one_flag_per_set_field(res: Resources) -> None:
    """Each requested field contributes exactly one flag, with gpus leading when > 0."""
    flags = build_sbatch_flags(res)
    optional = sum(x is not None for x in (res.walltime, res.queue, res.account, res.mem_gb))
    assert len(flags) == (1 if res.gpus else 0) + optional
    if res.gpus:
        assert flags[0] == f"--gpus={res.gpus}"


@pytest.mark.parametrize(
    ("state", "exit_code", "verdict"),
    [
        (None, None, "vanished"),
        ("R", None, "running"),
        ("Q", None, "running"),
        ("F", 0, "ok"),
        ("F", 1, "failed"),
        ("E", None, "unknown"),
        ("F", None, "unknown"),  # qdel'd while queued: finished, no Exit_status, never "ok"
    ],
)
def test_pbs_verdict(state: str | None, exit_code: int | None, verdict: str) -> None:
    """PBS verdict: gone -> vanished, non-terminal -> running, terminal -> by exit code."""
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


# A fixed revive moment; a task whose run began before it predates this daemon's lifetime.
REVIVE_AT = pendulum.datetime(2026, 6, 28, 12, 0, 0, tz="UTC")


@pytest.mark.parametrize(
    ("state", "start", "inherited"),
    [
        (
            PueueState.QUEUED,
            None,
            True,
        ),  # pueue requeues a crashed Running task, clearing its start
        (PueueState.PAUSED, None, True),  # paused after the crash-restart, no live process
        (PueueState.RUNNING, "2026-06-28T10:00:00+00:00", True),  # a Running task predating revive
        (
            PueueState.RUNNING,
            "2026-06-28T14:00:00+00:00",
            False,
        ),  # a genuine relaunch, started after
        (PueueState.DONE, "2026-06-28T10:00:00+00:00", False),  # terminal, never a zombie
    ],
)
def test_pueue_inherited(state: PueueState, start: str | None, inherited: bool) -> None:
    """An in-flight task the revived daemon did not itself start (no fresh start) reads as a
    zombie; a genuine relaunch with a start after the revive, and any finished task, do not."""
    task = PueueTask(id=1, state=state, start=start)
    assert pueue_inherited(task, REVIVE_AT) is inherited


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


def test_slurm_submit_default_resources_request_no_gpus(remote: RecordingMachine) -> None:
    """A default Resources() forces no `--gpus`, so CPU-only scripts run without GPU GRES."""
    remote.outputs = ["Submitted batch job 43\n"]
    Slurm().submit(remote, "/repo", "x.sh", ["--n", "1"], resources=Resources())
    assert "exec sbatch x.sh --n 1" in remote.calls[0][2]
    assert "--gpus" not in remote.calls[0][2]


class _FailingSbatchRemote:
    """A remote whose login shell exits non-zero: `.run(retcode=None)` must be used.

    Calling the command plumbum-style (which enforces retcode 0) raises, proving
    Slurm.submit tolerates the failure and reaches its own friendly SystemExit.
    """

    def __getitem__(self, _name: str) -> _FailingSbatchRemote:
        return self

    def run(self, retcode: int | None = 0) -> tuple[int, str, str]:
        assert retcode is None
        return (1, "", "sbatch: error: invalid account")

    def __call__(self, *_: object, **__: object) -> str:
        raise AssertionError("submit must use .run(retcode=None), not enforce retcode 0")


def test_slurm_submit_failure_raises_friendly_systemexit() -> None:
    """A non-zero remote sbatch surfaces the SystemExit check, not a ProcessExecutionError."""
    with pytest.raises(SystemExit, match=r"sbatch failed \(rc=1\).*invalid account"):
        Slurm().submit(_FailingSbatchRemote(), "/repo", "x.sh", [], resources=Resources())


def test_pbs_state_parses_record_into_jobstate(remote: RecordingMachine) -> None:
    """Pbs.state runs `info <handle>` and folds the record into a JobState with a verdict."""
    remote.outputs = ["Job Id: 7.s\n    job_state = F\n    Exit_status = 0\n"]
    state = Pbs().state(remote, "/repo", "7.s")
    assert isinstance(state, JobState)
    assert state.state == "F" and state.exit_code == 0 and state.verdict == "ok"
    assert "info 7.s" in remote.calls[0][2]


def test_slurm_state_runs_sacct_in_login_shell(remote: RecordingMachine) -> None:
    """Slurm.state runs `sacct` under `bash -lc` (the cluster toolchain) and parses its output."""
    remote.outputs = ["7|COMPLETED|0:0\n"]
    state = Slurm().state(remote, "/repo", "7")
    assert state.state == "COMPLETED" and state.verdict == "ok"
    assert remote.calls[0][:2] == ["bash", "-lc"] and remote.calls[0][2].startswith("sacct")


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
    """Pueue.jobs maps live tasks and omits completed queue history."""
    snapshot = {
        "tasks": {
            "0": {"id": 5, "label": "train", "status": {"Running": {}}},
            "1": {"id": 4, "label": "old", "status": {"Done": {"result": "Success"}}},
        },
    }
    remote.outputs = [json.dumps(snapshot)]
    [state] = Pueue().jobs(remote, "/repo")
    assert state.handle == "5" and state.label == "train" and state.verdict == "running"


@pytest.mark.parametrize(
    ("backend", "output", "command", "handle"),
    [
        (Pbs, "Job ID  Name  User  Time  S  Queue\n--\n7.s job1 u 00:01 R gpu\n", "qstat", "7.s"),
        (Slurm, "42|job1|RUNNING|gpu|00:05\n", "squeue", "42"),
    ],
)
def test_login_shell_jobs_parse_listing_into_states(
    backend: type, output: str, command: str, handle: str, remote: RecordingMachine
) -> None:
    """Pbs/Slurm.jobs run their listing command under `bash -lc` and map each row to a JobState."""
    remote.outputs = [output]
    [state] = backend().jobs(remote, "/repo")
    assert state.handle == handle and state.label == "job1" and state.verdict == "running"
    assert remote.calls[0][:2] == ["bash", "-lc"] and remote.calls[0][2].startswith(command)


def test_local_jobs_is_empty(remote: RecordingMachine) -> None:
    """The queue-less Local backend lists no live jobs."""
    assert Local().jobs(remote, "/repo") == []


# --- states (the one batched round-trip status resolves a host's pending runs with) ---


def test_slurm_states_keys_the_live_jobs_by_handle(remote: RecordingMachine) -> None:
    """Slurm.states lists `squeue` once and keys each live job by its handle for status to read."""
    remote.outputs = ["42|job1|RUNNING|gpu|00:05\n"]
    states = Slurm().states(remote, "/repo", ["42"])
    assert set(states) == {"42"} and states["42"].verdict == "running"


def test_local_states_is_empty(remote: RecordingMachine) -> None:
    """The queue-less Local backend resolves nothing in batch; status reads its verdict apart."""
    assert Local().states(remote, "/repo", ["x.sh"]) == {}


# --- revive (restart a dead scheduler daemon, then reconcile the zombies it inherits) ---


def test_pueue_revive_restarts_then_resumes_a_clean_queue() -> None:
    """Pueue.revive launches `pueued -d`, finds no zombies in a clean queue, and resumes the group
    so the revived host runs new work; it reports nothing cleared."""
    machine = RecordingMachine(["", "", json.dumps({"tasks": {}})])
    cleared = Pueue().revive(machine, "/repo")
    assert cleared == []
    assert machine.calls[:2] == [
        ["pueue", "shutdown"],
        ["sh", "-c", "pueued -d >/dev/null 2>&1"],
    ]
    assert ["pueue", "start", "--group", "default"] in machine.calls  # group un-paused at the end
    assert not any(call[:2] == ["pueue", "remove"] for call in machine.calls)  # nothing to remove


def test_pueue_revive_clears_inherited_zombies_but_spares_a_genuine_relaunch() -> None:
    """The fix: after the restart, every in-flight task predating the revive is a zombie whose real
    process died with the old daemon. A Running one is killed first so it can be removed, a
    requeued one is removed directly, both vanish, and a task genuinely relaunched after the revive
    is left alone. The finished history is untouched and the group is resumed."""
    snapshot = {
        "tasks": {
            "0": {"id": 5, "label": "done", "status": {"Done": {"result": "Success"}}},
            "1": {"id": 130, "label": "z", "status": {"Queued": {}}},  # requeued zombie
            "2": {  # a Running task whose start predates the revive: a zombie pueue kept Running
                "id": 201,
                "label": "z",
                "status": {"Running": {"start": "2000-01-01T00:00:00+00:00"}},
            },
            "3": {  # a genuine relaunch the new daemon started, far in the future of any revive
                "id": 200,
                "label": "live",
                "status": {"Running": {"start": "2099-01-01T00:00:00+00:00"}},
            },
        }
    }
    machine = RecordingMachine(["", "", json.dumps(snapshot)])
    cleared = Pueue().revive(machine, "/repo")
    assert cleared == ["130", "201"]  # both zombies retired, the live relaunch spared
    assert ["pueue", "kill", "201"] in machine.calls  # the Running zombie is killed before removal
    assert [
        "pueue",
        "remove",
        "130",
        "201",
    ] in machine.calls  # then both are removed (-> vanished)
    assert not any(
        "200" in call for call in machine.calls
    )  # the genuine relaunch is never touched
    assert ["pueue", "start", "--group", "default"] in machine.calls  # group resumed afterwards


@pytest.mark.parametrize("backend", [Pbs, Slurm, Local])
def test_site_managed_backends_have_no_daemon_to_revive(
    backend: type, remote: RecordingMachine
) -> None:
    """A cluster (PBS/SLURM) or bare-bash host has no user daemon, so revive is a clear error."""
    with pytest.raises(SystemExit, match="revive"):
        backend().revive(remote, "/repo")


# --- queues (the scheduler's node classes) ---


def test_pbs_queues_runs_qstat_q_in_login_shell(remote: RecordingMachine) -> None:
    """Pbs.queues enumerates `qstat -q` rows, including special classes like prepost."""
    remote.outputs = [
        "server: opbs00\n\n"
        "Queue            Memory CPU Time Walltime Node  Run Que Lm  State\n"
        "---------------- ------ -------- -------- ---- ---- ---- --  -----\n"
        "debug-g            --      --    00:30:00  --     0    0 --   E R\n"
        "regular-g          --      --    48:00:00  --    12   30 --   E R\n"
        "prepost            --      --    06:00:00  --     0    1 --   E R\n"
        "                                               ---- ----\n"
        "                                                 12   31\n"
    ]
    assert Pbs().queues(remote, "/repo") == ["debug-g", "regular-g", "prepost"]
    assert remote.calls[0][:2] == ["bash", "-lc"] and remote.calls[0][2] == "qstat -q"


def test_pbs_queues_falls_back_to_rsc_tree_when_q_is_rejected(
    remote: RecordingMachine,
) -> None:
    """Miyabi's qstat wrapper rejects -q, so queues falls through to --rsc."""
    remote.outputs = [
        "usage: qstat -a [-v] [-t]\nqstat: error: unrecognized arguments: -q\n",
        "SYSTEM: Miyabi-G\n"
        "QUEUE                     STATUS                 NODE\n"
        "debug-g                   [ENABLE, START]          48\n"
        "regular-g\n"
        "  |-- small-g             [ENABLE, START]        1024\n",
    ]
    assert Pbs().queues(remote, "/repo") == ["debug-g", "small-g"]
    assert remote.calls[1][2] == "qstat --rsc"


def test_slurm_queues_runs_sinfo_in_login_shell(remote: RecordingMachine) -> None:
    """Slurm.queues enumerates `sinfo` partitions, default marker stripped and deduped."""
    remote.outputs = ["gpu*\ngpu*\ncpu\nprepost\n"]
    assert Slurm().queues(remote, "/repo") == ["gpu", "cpu", "prepost"]
    assert remote.calls[0][:2] == ["bash", "-lc"] and remote.calls[0][2].startswith("sinfo")


def test_pueue_and_local_have_no_queues(remote: RecordingMachine) -> None:
    """The single-machine backends report no node classes beyond the login probe."""
    assert Pueue().queues(remote, "/repo") == []
    assert Local().queues(remote, "/repo") == []
    assert remote.calls == []  # nothing asked of the host


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


class TransportFailingMachine:
    """A remote whose login shell fails at the ssh transport: `.run(retcode=None)` yields ssh's
    255 with a transport phrase in stderr, the `Session open refused by peer` we hit on Miyabi."""

    def __init__(self, stderr: str = "mux_client_request_session: Session open refused by peer"):
        self.stderr = stderr

    def __getitem__(self, _name: str) -> TransportFailingMachine:
        return self

    def run(self, *_: object, **__: object) -> tuple[int, str, str]:
        return (255, "", self.stderr)


def test_login_run_raises_host_unreachable_on_ssh_transport_failure() -> None:
    """A refused/dropped ssh session (exit 255 + a transport phrase) raises HostUnreachable,
    so a probe never reads the resulting empty output as a finished or vanished job."""
    with pytest.raises(HostUnreachable):
        login_run(TransportFailingMachine(), "qstat -f 7")


def test_login_run_returns_stdout_when_the_command_actually_ran(remote: RecordingMachine) -> None:
    """A command that ran and exited (even non-zero, e.g. qstat 'unknown job id') is a real
    answer, not a transport failure, so its stdout flows back to the parser unchanged."""
    remote.outputs = ["Job Id: 7.s\n    job_state = F\n"]
    assert "job_state = F" in login_run(remote, "qstat -f 7")


def test_poll_until_done_absorbs_a_transient_unreachable_then_finishes() -> None:
    """A HostUnreachable mid-wait is a blip, not a verdict: the loop backs off, retries, and
    returns the real terminal state once the host answers again -- never a false `vanished`."""
    answers: list[object] = [
        JobState(handle="1", verdict="running"),
        HostUnreachable("Session open refused by peer"),
        JobState(handle="1", exit_code=0, verdict="ok"),
    ]

    def probe() -> JobState:
        answer = answers.pop(0)
        if isinstance(answer, HostUnreachable):
            raise answer
        return answer

    slept: list[float] = []
    final = poll_until_done(probe, interval=1.0, sleeper=slept.append)
    assert final.verdict == "ok"
    assert slept == [1.0, 1.0]  # one running-poll sleep, one backoff sleep for the blip


def test_poll_until_done_reraises_once_the_host_stays_unreachable() -> None:
    """A persistent outage past the retry budget surfaces as HostUnreachable, so a genuinely
    down host is reported rather than silently waited on forever."""

    def probe() -> JobState:
        raise HostUnreachable("host down")

    with pytest.raises(HostUnreachable):
        poll_until_done(probe, interval=0.0, sleeper=lambda _: None, retries=3)


def test_pbs_state_surfaces_transport_failure_instead_of_vanished() -> None:
    """The original bug: a refused ssh session made Pbs.state parse empty output to the terminal
    `vanished` verdict, ending the wait early. Now it raises HostUnreachable for the loop to retry.
    """
    with pytest.raises(HostUnreachable):
        Pbs().state(TransportFailingMachine(), "/repo", "7.s")


def test_pueue_wait_polls_state(remote: RecordingMachine) -> None:
    """Pueue.wait blocks on the task's state until it is terminal."""
    snapshot = {"tasks": {"0": {"id": 9, "label": "t", "status": {"Done": {"result": "Success"}}}}}
    remote.outputs = [json.dumps(snapshot)]
    final = Pueue().wait(remote, "/repo", "9")
    assert final.verdict == "ok"


def test_local_wait_returns_ok_at_once(remote: RecordingMachine) -> None:
    """Local.wait does not poll: submit already ran the job in the foreground and raised
    on failure, so reaching wait means the run finished fine."""
    assert Local().wait(remote, "/repo", "x.sh").verdict == "ok"


def test_pbs_wait_blocks_on_state(remote: RecordingMachine) -> None:
    """Pbs.wait polls the job's `info` record until it is terminal."""
    remote.outputs = ["Job Id: 7.s\n    job_state = F\n    Exit_status = 0\n"]
    assert Pbs().wait(remote, "/repo", "7.s").verdict == "ok"


def test_pbs_states_batches_live_and_finished_with_exit_codes(remote: RecordingMachine) -> None:
    """One `qstat -f -H <handles>` resolves a whole host: a running job and a finished one (with
    its exit status) come back keyed by handle, so finished jobs need no per-run probe."""
    remote.outputs = [
        "Job Id: 1.s\n    job_state = R\n"
        "Job Id: 2.s\n    job_state = F\n    Exit_status = 0\n"
        "Job Id: 3.s\n    job_state = F\n    Exit_status = 1\n"
    ]
    states = Pbs().states(remote, "/repo", ["1.s", "2.s", "3.s"])
    assert "qstat -f -H 1.s 2.s 3.s" in " ".join(remote.calls[-1])  # one batched call
    assert states["1.s"].verdict == "running"
    assert states["2.s"].verdict == "ok" and states["2.s"].exit_code == 0
    assert states["3.s"].verdict == "failed" and states["3.s"].exit_code == 1


def test_pbs_states_skips_the_host_when_nothing_is_pending(remote: RecordingMachine) -> None:
    """No handles to resolve means no ssh at all (an all-terminal host touches nothing)."""
    assert Pbs().states(remote, "/repo", []) == {}
    assert remote.calls == []


def test_pueue_states_keys_every_task_by_handle(remote: RecordingMachine) -> None:
    """Pueue.states returns one entry per task (a single `pueue status` already lists finished)."""
    snapshot = {"tasks": {"0": {"id": 9, "label": "t", "status": {"Done": {"result": "Success"}}}}}
    remote.outputs = [json.dumps(snapshot)]
    states = Pueue().states(remote, "/repo", ["9"])
    assert states["9"].verdict == "ok"


def test_slurm_wait_blocks_on_state(remote: RecordingMachine) -> None:
    """Slurm.wait polls `sacct` until the job is terminal."""
    remote.outputs = ["7|COMPLETED|0:0\n"]
    assert Slurm().wait(remote, "/repo", "7").verdict == "ok"


# --- stream (synchronous log relay until terminal) ---


def test_stream_until_done_drains_between_polls_then_once_more() -> None:
    """stream_until_done drains from the running offset each tick and once after the end."""
    states = [
        JobState(handle="1", verdict="running"),
        JobState(handle="1", verdict="running"),
        JobState(handle="1", exit_code=0, verdict="ok"),
    ]
    chunks = [5, 3, 2]  # bytes "printed" per drain call
    offsets: list[int] = []

    def drain(offset: int) -> int:
        offsets.append(offset)
        return chunks[len(offsets) - 1]

    slept: list[float] = []
    final = stream_until_done(lambda: states.pop(0), drain, interval=1.0, sleeper=slept.append)
    assert final.verdict == "ok"
    assert offsets == [0, 5, 8]  # each drain resumes where the previous one stopped
    assert slept == [1.0, 1.0]  # one sleep per running poll, none after the final drain


def test_drain_log_prints_chunk_and_returns_byte_count(
    remote: RecordingMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    """drain_log runs `lote exec logs --offset N` in a login shell and relays its stdout."""
    remote.outputs = ["hello\n"]
    consumed = drain_log(remote, "/repo", "7", 42)
    assert consumed == len(b"hello\n")
    assert capsys.readouterr().out == "hello\n"
    [call] = remote.calls
    assert call[:2] == ["bash", "-lc"]
    assert "lote exec logs 7 --offset 42" in call[2]


def test_pbs_stream_drains_after_terminal_state(
    remote: RecordingMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pbs.stream polls state, then drains the log once more after the job finishes."""
    remote.outputs = ["Job Id: 7.s\n    job_state = F\n    Exit_status = 0\n", "tail text\n"]
    final = Pbs().stream(remote, "/repo", "7.s")
    assert final.verdict == "ok"
    assert capsys.readouterr().out == "tail text\n"
    assert "info 7.s" in remote.calls[0][2]
    assert "logs 7.s --offset 0" in remote.calls[1][2]


def test_slurm_stream_drains_after_terminal_state(
    remote: RecordingMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    """Slurm.stream mirrors Pbs: poll sacct state, then drain the captured log."""
    remote.outputs = ["7|COMPLETED|0:0\n", "done\n"]
    final = Slurm().stream(remote, "/repo", "7")
    assert final.verdict == "ok"
    assert capsys.readouterr().out == "done\n"
    assert "logs 7 --offset 0" in remote.calls[1][2]


def test_pueue_stream_follows_natively_then_reports(remote: RecordingMachine) -> None:
    """Pueue.stream rides `pueue follow` (which exits at task end) then reads the verdict."""
    snapshot = {"tasks": {"0": {"id": 9, "label": "t", "status": {"Done": {"result": "Success"}}}}}
    remote.outputs = ["", json.dumps(snapshot)]
    final = Pueue().stream(remote, "/repo", "9")
    assert final.verdict == "ok"
    assert remote.calls[0][:2] == ["pueue", "follow"]


def test_pueue_stream_treats_a_removed_follow_target_as_vanished(
    remote: RecordingMachine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pueue, "binary", lambda *args: local["sh"][["-c", "exit 1"]])
    monkeypatch.setattr(pueue, "status", lambda **kwargs: [])

    assert Pueue().stream(remote, "/repo", "9").verdict == "vanished"


def test_pueue_stream_preserves_a_real_follow_failure(
    remote: RecordingMachine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pueue, "binary", lambda *args: local["sh"][["-c", "exit 1"]])
    monkeypatch.setattr(
        pueue,
        "status",
        lambda **kwargs: [PueueTask(id=9, state=PueueState.RUNNING)],
    )

    with pytest.raises(ProcessExecutionError):
        Pueue().stream(remote, "/repo", "9")


def test_local_stream_returns_ok_without_following(remote: RecordingMachine) -> None:
    """Local.stream has nothing to follow: submit already relayed the output."""
    assert Local().stream(remote, "/repo", "x.sh").verdict == "ok"
    assert remote.calls == []
