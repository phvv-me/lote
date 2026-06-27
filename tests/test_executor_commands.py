from pathlib import Path

import pytest

import lote.executor.cli as exec_cli
from lote.clients.pbs import JobInfo, PbsState
from lote.clients.slurm import SlurmJob, SlurmState
from lote.executor.cli import Executor

from .conftest import fake_group


@pytest.fixture
def captured_qsub(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub the qsub seam and the group lookup; yields the kwargs the Executor builds."""
    captured: dict[str, object] = {}

    def fake_qsub(resources: object, **kw: object) -> str:
        captured.update(kw)
        captured["resources"] = resources
        return "777.srv"

    monkeypatch.setattr(exec_cli, "qsub", fake_qsub)
    monkeypatch.setattr(exec_cli.grp, "getgrgid", lambda _gid: fake_group("grp"))
    monkeypatch.setattr(exec_cli.os, "getgid", lambda: 0)
    return captured


@pytest.fixture
def captured_sbatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub the sbatch seam; yields the kwargs the Executor builds."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli, "sbatch", lambda **kw: captured.update(kw) or "42")
    return captured


def pbs_script(tmp_path: Path) -> Path:
    """A job script carrying #PBS directives the qsub command method parses."""
    path = tmp_path / "experiments" / "exp" / "jobs" / "train.sh"
    path.parent.mkdir(parents=True)
    path.write_text(
        "#!/bin/bash\n"
        "#PBS -N trainjob\n"
        "#PBS -q gen-S\n"
        "#PBS -l select=2:ncpus=4\n"
        "#PBS -l walltime=03:00:00\n"
        "echo run\n"
    )
    return path


def sbatch_script(tmp_path: Path) -> Path:
    """A job script carrying #SBATCH directives the sbatch command method parses."""
    path = tmp_path / "experiments" / "exp" / "jobs" / "train.sh"
    path.parent.mkdir(parents=True)
    path.write_text(
        "#!/bin/bash\n"
        "#SBATCH --job-name=trainjob\n"
        "#SBATCH --time=03:00:00\n"
        "#SBATCH --partition=gpu\n"
        "#SBATCH --gpus=2\n"
        "#SBATCH --mem=64G\n"
        "echo run\n"
    )
    return path


def test_qsub_folds_directives_into_call(tmp_path: Path, captured_qsub: dict[str, object]) -> None:
    """qsub parses #PBS directives, makes the logs dir, and forwards args via ARGS."""
    script = pbs_script(tmp_path)

    handle = Executor().qsub(str(script), "--lr", "0.1")

    resources = captured_qsub["resources"]
    assert handle == "777.srv"
    assert captured_qsub["queue"] == "gen-S"  # from `#PBS -q`
    assert resources.walltime == "03:00:00"  # parsed from the -l line
    assert resources.select == "2:ncpus=4"  # parsed select clause
    assert captured_qsub["job_name"] == "trainjob"  # from `#PBS -N`
    assert captured_qsub["group_list"] == "grp"  # defaulted from the user's group
    assert captured_qsub["variable_list"] == {"ARGS": "--lr 0.1"}
    assert (script.parent.parent / "logs" / "trainjob").is_dir()


def test_qsub_reads_select_directive_when_not_overridden(
    tmp_path: Path, captured_qsub: dict[str, object]
) -> None:
    """A `#PBS -l select=` line is parsed into the select arg when no override is given."""
    path = tmp_path / "s.sh"
    path.write_text("#!/bin/bash\n#PBS -l select=8\n#PBS -l walltime=05:00:00\necho hi\n")
    Executor().qsub(str(path))
    resources = captured_qsub["resources"]
    assert resources.select == "8"  # taken from the directive
    assert resources.walltime == "05:00:00"


def test_qsub_overrides_and_default_select(
    tmp_path: Path, captured_qsub: dict[str, object]
) -> None:
    """Explicit flags win over directives; with no positional args there is no var list."""
    path = tmp_path / "bare.sh"
    path.write_text("#!/bin/bash\n#PBS -N bare\necho hi\n")
    Executor().qsub(str(path), queue="debug", walltime="01:00:00", select=4, group_list="acct")

    resources = captured_qsub["resources"]
    assert captured_qsub["queue"] == "debug"
    assert resources.walltime == "01:00:00"
    assert resources.select == 4
    assert captured_qsub["group_list"] == "acct"
    assert captured_qsub["variable_list"] is None  # no positional args


def test_qsub_defaults_select_to_one(tmp_path: Path, captured_qsub: dict[str, object]) -> None:
    """With no select directive and no --select override, select defaults to 1."""
    path = tmp_path / "bare.sh"
    path.write_text("#!/bin/bash\necho hi\n")
    Executor().qsub(str(path))
    assert captured_qsub["resources"].select == 1


def test_sbatch_folds_directives_into_call(
    tmp_path: Path, captured_sbatch: dict[str, object]
) -> None:
    """sbatch parses #SBATCH directives, makes the logs dir, and forwards args via ARGS."""
    script = sbatch_script(tmp_path)

    handle = Executor().sbatch(str(script), "--seed", "1")

    assert handle == "42"
    assert captured_sbatch["job_name"] == "trainjob"
    assert captured_sbatch["walltime"] == "03:00:00"
    assert captured_sbatch["partition"] == "gpu"
    assert captured_sbatch["gpus"] == 2  # parsed leading int from `--gpus=2`
    assert captured_sbatch["mem_gb"] == 64  # parsed leading int from `--mem=64G`
    assert captured_sbatch["export_vars"] == {"ARGS": "--seed 1"}
    assert str(captured_sbatch["output_path"]).endswith("/logs/trainjob/%j.log")


def test_sbatch_defaults_when_no_directives(
    tmp_path: Path, captured_sbatch: dict[str, object]
) -> None:
    """A bare script requests no GPUs (CPU-only friendly), leaves the rest None, no export."""
    path = tmp_path / "bare.sh"
    path.write_text("#!/bin/bash\necho hi\n")

    Executor().sbatch(str(path))

    assert captured_sbatch["gpus"] is None  # omitted unless requested by flag or directive
    assert captured_sbatch["walltime"] is None
    assert captured_sbatch["partition"] is None
    assert captured_sbatch["mem_gb"] is None
    assert captured_sbatch["export_vars"] is None
    assert captured_sbatch["job_name"] == "bare"  # falls back to the file stem


def test_run_invokes_bash_with_args_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run shells the script through bash with ARGS set and returns nothing on success."""
    path = tmp_path / "go.sh"
    path.write_text("echo hi\n")
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], *, env: dict[str, str], check: bool):  # noqa: ANN202
        captured["cmd"] = cmd
        captured["args"] = env["ARGS"]
        return type("R", (), {"returncode": 0})

    monkeypatch.setattr(exec_cli.subprocess, "run", fake_run)

    assert Executor().run(str(path), "a", "b c") is None

    assert captured["cmd"] == ["bash", str(path)]
    assert captured["args"] == "a 'b c'"  # shlex-quoted


def test_run_failing_script_raises_systemexit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero script exit becomes SystemExit with that code (fire would mask an int)."""
    path = tmp_path / "go.sh"
    path.write_text("exit 3\n")
    monkeypatch.setattr(
        exec_cli.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 3})
    )

    with pytest.raises(SystemExit) as excinfo:
        Executor().run(str(path))
    assert excinfo.value.code == 3


def test_status_slurm_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """With squeue present, status renders the SLURM table; the empty list prints `no jobs`."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: name == "squeue")
    jobs = [SlurmJob(job_id="1", name="j", state=SlurmState.RUNNING, partition="gpu")]
    monkeypatch.setattr(exec_cli, "squeue", lambda **_: jobs)
    rendered: list[object] = []
    monkeypatch.setattr(exec_cli, "_print_slurm_table", lambda j, *, console: rendered.append(j))

    Executor().status()
    assert rendered == [jobs]

    monkeypatch.setattr(exec_cli, "squeue", lambda **_: [])
    Executor().status()  # empty -> early `no jobs`, no second render
    assert rendered == [jobs]


def test_status_pbs_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without squeue, status uses qstat; a list renders the PBS table, empty prints `no jobs`."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
    jobs = [JobInfo(job_id="1.s", name="j", user="u", state=PbsState.RUNNING, queue="q")]
    monkeypatch.setattr(exec_cli, "qstat", lambda **_: jobs)
    rendered: list[object] = []
    monkeypatch.setattr(exec_cli, "_print_jobs_table", lambda j, *, console: rendered.append(j))

    Executor().status(all_users=True)
    assert rendered == [jobs]

    monkeypatch.setattr(exec_cli, "qstat", lambda **_: [])
    Executor().status()
    assert rendered == [jobs]


def test_status_pbs_unparsed_string(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When qstat returns a raw string (parse failure fallback), status prints it verbatim."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
    monkeypatch.setattr(exec_cli, "qstat", lambda **_: "raw qstat text")
    Executor().status()
    assert "raw qstat text" in capsys.readouterr().out


def test_info_slurm_and_pbs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """info prints sacct output when sacct exists, else the raw qstat -f record."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: name == "sacct")
    monkeypatch.setattr(exec_cli, "sacct", lambda jid, *, parse_output: f"sacct:{jid}")
    Executor().info("7")
    assert "sacct:7" in capsys.readouterr().out

    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
    monkeypatch.setattr(exec_cli, "qstat", lambda **kw: f"qstat-f:{kw['job_ids']}")
    Executor().info("8")
    assert "qstat-f:8" in capsys.readouterr().out


def test_info_pbs_prints_live_record_when_job_is_live(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A still-live PBS job is read from `qstat` once and printed without the history fallback."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
    history_calls: list[bool] = []
    monkeypatch.setattr(
        exec_cli,
        "qstat",
        lambda **kw: history_calls.append(kw["history"]) or "Job Id: 9.pbs\n",
    )
    Executor().info("9")
    assert "Job Id: 9.pbs" in capsys.readouterr().out
    assert history_calls == [False]


def test_info_pbs_finished_job_falls_through_to_history(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finished job (live qstat rejects the id, tolerated via retcode=None) reads history."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
    seen: list[tuple[bool, int | None]] = []

    def fake_qstat(**kw):  # noqa: ANN003, ANN202
        seen.append((kw["history"], kw["retcode"]))
        return "Job Id: 9.pbs (finished)\n" if kw["history"] else ""

    monkeypatch.setattr(exec_cli, "qstat", fake_qstat)
    Executor().info("9")
    assert "Job Id: 9.pbs (finished)" in capsys.readouterr().out
    # live first, then the -H history fallback, both tolerating a non-zero qstat exit.
    assert seen == [(False, None), (True, None)]


def test_logs_globs_and_tails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """logs finds the newest matching log under experiments/ and tails it."""
    logs = tmp_path / "experiments" / "exp" / "logs" / "trainjob"
    logs.mkdir(parents=True)
    target_log = logs / "999.log"
    target_log.write_text("x\n")
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli.subprocess, "run", lambda cmd, *, check: captured.update(cmd=cmd))

    Executor().logs("999", lines=10)
    assert captured["cmd"] == ["tail", "-n10", str(target_log)]


def test_logs_prefers_research_root_over_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """logs finds a match under the research projects tree first, never globbing the CWD."""
    logs = tmp_path / "projects" / "tb" / "experiments" / "exp" / "logs" / "trainjob"
    logs.mkdir(parents=True)
    target_log = logs / "777.log"
    target_log.write_text("x\n")
    monkeypatch.setattr(exec_cli, "experiments_root", lambda: tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli.subprocess, "run", lambda cmd, *, check: captured.update(cmd=cmd))

    Executor().logs("777", lines=20)
    assert captured["cmd"] == ["tail", "-n20", str(target_log)]


def test_logs_direct_path_with_follow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that exists is tailed directly, and --follow adds -f."""
    log = tmp_path / "j.log"
    log.write_text("y\n")
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli.subprocess, "run", lambda cmd, *, check: captured.update(cmd=cmd))
    Executor().logs(str(log), follow=True, lines=5)
    assert captured["cmd"] == ["tail", "-n5", "-f", str(log)]


def test_logs_missing_prints_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No matching log yet prints a friendly note (queued job is normal) rather than raising."""
    monkeypatch.chdir(tmp_path)
    Executor().logs("nope")
    assert "no log yet for 'nope'" in capsys.readouterr().out


def test_logs_offset_prints_only_new_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`logs --offset N` prints the log's content from byte N on (the `lote run` poll seam)."""
    log = tmp_path / "j.log"
    log.write_text("first\nsecond\n")
    Executor().logs(str(log), offset=len(b"first\n"))
    assert capsys.readouterr().out == "second\n"
    Executor().logs(str(log), offset=len(b"first\nsecond\n"))
    assert capsys.readouterr().out == ""  # nothing new past the end


def test_logs_offset_with_no_log_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A queued job has no log yet: the offset read stays silent so the stream stays clean."""
    monkeypatch.chdir(tmp_path)
    Executor().logs("queued-job", offset=0)
    assert capsys.readouterr().out == ""


def test_cancel_pbs_by_name_all_nomatch_and_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PBS branch resolves names/all via qstat then qdels; no match warns; bad raises."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
    jobs = [
        JobInfo(job_id="1.s", name="train", user="u", state=PbsState.RUNNING, queue="q"),
        JobInfo(job_id="2.s", name="eval", user="u", state=PbsState.QUEUED, queue="q"),
    ]
    monkeypatch.setattr(exec_cli, "qstat", lambda *a, **k: jobs)
    deleted: list[str] = []
    monkeypatch.setattr(exec_cli, "qdel", lambda jid, *, force: deleted.append(jid))

    Executor().cancel("train")
    assert deleted == ["1.s"]

    deleted.clear()
    Executor().cancel("all", force=True)
    assert deleted == ["1.s", "2.s"]

    deleted.clear()
    monkeypatch.setattr(exec_cli, "qstat", lambda *a, **k: [])
    Executor().cancel("ghost")
    assert deleted == []

    monkeypatch.setattr(exec_cli, "qstat", lambda *a, **k: "garbage")
    with pytest.raises(RuntimeError, match="qstat parsing failed"):
        Executor().cancel("x")


def test_has_command_uses_command_v(monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_command shells `command -v <name>` and maps a zero return code to True."""
    seen: dict[str, object] = {}

    def fake_run(cmd, *, capture_output):  # noqa: ANN001, ANN202
        seen["cmd"] = cmd
        return type("R", (), {"returncode": 0})

    monkeypatch.setattr(exec_cli.subprocess, "run", fake_run)
    assert exec_cli._has_command("squeue") is True
    assert seen["cmd"][:2] == ["bash", "-lc"]
    assert "command -v squeue" in seen["cmd"][2]

    monkeypatch.setattr(
        exec_cli.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})
    )
    assert exec_cli._has_command("squeue") is False


def test_exec_app_registers_every_command() -> None:
    """The `lote exec` cyclopts app carries each executor command by name."""
    assert {"qsub", "sbatch", "run", "status", "info", "logs", "cancel"} <= set(exec_cli.app)


def test_cancel_slurm_by_name_all_and_nomatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SLURM branch resolves names/all via squeue then scancels; no match warns; bad raises."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: name == "scancel")
    jobs = [
        SlurmJob(job_id="10", name="train", state=SlurmState.RUNNING),
        SlurmJob(job_id="11", name="eval", state=SlurmState.PENDING),
    ]
    monkeypatch.setattr(exec_cli, "squeue", lambda **_: jobs)
    cancelled: list[str] = []
    monkeypatch.setattr(exec_cli, "scancel", lambda jid: cancelled.append(jid))

    Executor().cancel("train")
    assert cancelled == ["10"]

    cancelled.clear()
    Executor().cancel("all")
    assert cancelled == ["10", "11"]

    cancelled.clear()
    Executor().cancel("ghost")
    assert cancelled == []

    monkeypatch.setattr(exec_cli, "squeue", lambda **_: "garbage")
    with pytest.raises(RuntimeError, match="squeue parsing failed"):
        Executor().cancel("x")
