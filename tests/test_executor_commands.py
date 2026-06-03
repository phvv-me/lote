from __future__ import annotations

from pathlib import Path

import pytest

import fleet.executor.cli as exec_cli
from fleet.clients.pbs import JobInfo, JobState
from fleet.clients.slurm import SlurmJob, SlurmState
from fleet.executor.cli import Executor


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


def test_qsub_folds_directives_into_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """qsub parses #PBS directives, makes the logs dir, and forwards args via ARGS."""
    script = pbs_script(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli, "qsub", lambda **kw: captured.update(kw) or "777.srv")
    monkeypatch.setattr(exec_cli.grp, "getgrgid", lambda _gid: type("G", (), {"gr_name": "grp"}))
    monkeypatch.setattr(exec_cli.os, "getgid", lambda: 0)

    handle = Executor().qsub(str(script), "--lr", "0.1")

    assert handle == "777.srv"
    assert captured["queue"] == "gen-S"  # from `#PBS -q`
    assert captured["walltime"] == "03:00:00"  # parsed from the -l line
    assert captured["select"] == "2:ncpus=4"  # parsed select clause
    assert captured["job_name"] == "trainjob"  # from `#PBS -N`
    assert captured["group_list"] == "grp"  # defaulted from the user's group
    assert captured["variable_list"] == {"ARGS": "--lr 0.1"}
    assert (script.parent.parent / "logs" / "trainjob").is_dir()


def test_qsub_reads_select_directive_when_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `#PBS -l select=` line is parsed into the select arg when no override is given."""
    path = tmp_path / "s.sh"
    path.write_text("#!/bin/bash\n#PBS -l select=8\n#PBS -l walltime=05:00:00\necho hi\n")
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli, "qsub", lambda **kw: captured.update(kw) or "1")
    monkeypatch.setattr(exec_cli.grp, "getgrgid", lambda _gid: type("G", (), {"gr_name": "g"}))
    monkeypatch.setattr(exec_cli.os, "getgid", lambda: 0)

    Executor().qsub(str(path))
    assert captured["select"] == "8"  # taken from the directive
    assert captured["walltime"] == "05:00:00"


def test_qsub_overrides_and_default_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit flags win over directives; with no positional args there is no var list."""
    path = tmp_path / "bare.sh"
    path.write_text("#!/bin/bash\n#PBS -N bare\necho hi\n")
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli, "qsub", lambda **kw: captured.update(kw) or "1")
    monkeypatch.setattr(exec_cli.grp, "getgrgid", lambda _gid: type("G", (), {"gr_name": "g"}))
    monkeypatch.setattr(exec_cli.os, "getgid", lambda: 0)

    Executor().qsub(str(path), queue="debug", walltime="01:00:00", select=4, group_list="acct")

    assert captured["queue"] == "debug"
    assert captured["walltime"] == "01:00:00"
    assert captured["select"] == 4
    assert captured["group_list"] == "acct"
    assert captured["variable_list"] is None  # no positional args


def test_qsub_defaults_select_to_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no select directive and no --select override, select defaults to 1."""
    path = tmp_path / "bare.sh"
    path.write_text("#!/bin/bash\necho hi\n")
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli, "qsub", lambda **kw: captured.update(kw) or "1")
    monkeypatch.setattr(exec_cli.grp, "getgrgid", lambda _gid: type("G", (), {"gr_name": "g"}))
    monkeypatch.setattr(exec_cli.os, "getgid", lambda: 0)
    Executor().qsub(str(path))
    assert captured["select"] == 1


def test_sbatch_folds_directives_into_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sbatch parses #SBATCH directives, makes the logs dir, and forwards args via ARGS."""
    script = sbatch_script(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli, "sbatch", lambda **kw: captured.update(kw) or "42")

    handle = Executor().sbatch(str(script), "--seed", "1")

    assert handle == "42"
    assert captured["job_name"] == "trainjob"
    assert captured["walltime"] == "03:00:00"
    assert captured["partition"] == "gpu"
    assert captured["gpus"] == 2  # parsed leading int from `--gpus=2`
    assert captured["mem_gb"] == 64  # parsed leading int from `--mem=64G`
    assert captured["export_vars"] == {"ARGS": "--seed 1"}
    assert str(captured["output_path"]).endswith("/logs/trainjob/%j.log")


def test_sbatch_defaults_when_no_directives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare script defaults gpus to 1, leaves the rest None, and emits no export when no args."""
    path = tmp_path / "bare.sh"
    path.write_text("#!/bin/bash\necho hi\n")
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli, "sbatch", lambda **kw: captured.update(kw) or "9")

    Executor().sbatch(str(path))

    assert captured["gpus"] == 1
    assert captured["walltime"] is None
    assert captured["partition"] is None
    assert captured["mem_gb"] is None
    assert captured["export_vars"] is None
    assert captured["job_name"] == "bare"  # falls back to the file stem


def test_run_invokes_bash_with_args_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run shells the script through bash with ARGS set, returning the process exit code."""
    path = tmp_path / "go.sh"
    path.write_text("echo hi\n")
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], *, env: dict[str, str], check: bool):  # noqa: ANN202
        captured["cmd"] = cmd
        captured["args"] = env["ARGS"]
        return type("R", (), {"returncode": 3})

    monkeypatch.setattr(exec_cli.subprocess, "run", fake_run)

    code = Executor().run(str(path), "a", "b c")

    assert code == 3
    assert captured["cmd"] == ["bash", str(path)]
    assert captured["args"] == "a 'b c'"  # shlex-quoted


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
    jobs = [JobInfo(job_id="1.s", name="j", user="u", state=JobState.RUNNING, queue="q")]
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


def test_logs_direct_path_with_follow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that exists is tailed directly, and --follow adds -f."""
    log = tmp_path / "j.log"
    log.write_text("y\n")
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_cli.subprocess, "run", lambda cmd, *, check: captured.update(cmd=cmd))
    Executor().logs(str(log), follow=True, lines=5)
    assert captured["cmd"] == ["tail", "-n5", "-f", str(log)]


def test_logs_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No matching log anywhere raises a clear FileNotFoundError."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="no log for"):
        Executor().logs("nope")


def test_cancel_pbs_by_name_and_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """cancel resolves names through qstat then qdels each match; `all` cancels every job."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
    jobs = [
        JobInfo(job_id="1.s", name="train", user="u", state=JobState.RUNNING, queue="q"),
        JobInfo(job_id="2.s", name="eval", user="u", state=JobState.QUEUED, queue="q"),
    ]
    monkeypatch.setattr(exec_cli, "qstat", lambda *a, **k: jobs)
    deleted: list[str] = []
    monkeypatch.setattr(exec_cli, "qdel", lambda jid, *, force: deleted.append(jid))

    Executor().cancel("train")
    assert deleted == ["1.s"]

    deleted.clear()
    Executor().cancel("all", force=True)
    assert deleted == ["1.s", "2.s"]


def test_cancel_pbs_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unmatched target prints a warning and qdels nothing."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
    monkeypatch.setattr(exec_cli, "qstat", lambda *a, **k: [])
    called: list[str] = []
    monkeypatch.setattr(exec_cli, "qdel", lambda *a, **k: called.append("x"))
    Executor().cancel("ghost")
    assert called == []


def test_cancel_pbs_parse_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-list qstat (parse failure) is a hard error, not a silent no-op."""
    monkeypatch.setattr(exec_cli, "_has_command", lambda name: False)
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


def test_main_invokes_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `python -m fleet.executor.cli` entry point hands the Executor class to fire.Fire."""
    captured: list[object] = []
    monkeypatch.setattr(exec_cli.fire, "Fire", lambda obj: captured.append(obj))
    exec_cli.main()
    assert captured == [Executor]


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
