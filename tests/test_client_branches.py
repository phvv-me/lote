from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from fleet.clients import pueue
from fleet.clients.pbs import qdel, qstat
from fleet.clients.pbs.job_info import JobInfo
from fleet.clients.pbs.job_state import JobState
from fleet.clients.pbs.qsub import qsub
from fleet.clients.pbs.resource_spec import ResourceSpec
from fleet.clients.rsync import Rsync, rsync
from fleet.clients.slurm import SlurmJob, SlurmState, sacct, sbatch, scancel, squeue
from fleet.executor.cli import _print_jobs_table, _print_slurm_table, experiments_root
from fleet.executor.local import ensure_job_local_root, get_job_local_root
from fleet.schedulers import Local, Pbs, Pueue, Resources, Slurm

from .conftest import RecordingMachine

# --- client runners (the non-dry-run, machine-bound path) ---


def machine_with(*outputs: str) -> RecordingMachine:
    """A recording machine queued with these stdout strings, one per command call."""
    return RecordingMachine(list(outputs))


def test_squeue_runs_and_parses() -> None:
    """squeue runs on the machine and parses the pipe output into SlurmJob rows."""
    machine = machine_with("1|train|RUNNING|gpu|00:01:00\n")
    [job] = squeue(machine=machine)
    assert job.job_id == "1" and job.state is SlurmState.RUNNING
    assert machine.calls[0][0] == "squeue"


def test_squeue_unparsed_returns_raw() -> None:
    """With parse_output=False squeue returns the raw stdout string."""
    machine = machine_with("raw\n")
    assert squeue(machine=machine, parse_output=False) == "raw\n"


def test_squeue_dry_run_renders() -> None:
    """squeue(dry_run=True) renders the command without touching a machine; me=False drops --me."""
    assert squeue(dry_run=True, job_id="9").startswith("squeue --noheader")
    assert "--me" not in squeue(dry_run=True, me=False)


def test_sacct_runs_and_parses() -> None:
    """sacct runs and parses the top-level row for the requested id."""
    machine = machine_with("7|COMPLETED|0:0\n")
    job = sacct("7", machine=machine)
    assert isinstance(job, SlurmJob) and job.state is SlurmState.COMPLETED


def test_sacct_unparsed_and_dry_run() -> None:
    """sacct returns raw stdout when parse_output=False and renders under dry_run."""
    assert sacct("7", machine=machine_with("raw\n"), parse_output=False) == "raw\n"
    assert sacct("7", dry_run=True).startswith("sacct --jobs 7")


def test_sbatch_runs_and_extracts_id() -> None:
    """sbatch runs on the machine and extracts the submitted job id."""
    machine = machine_with("Submitted batch job 555\n")
    assert sbatch(script="/x.sh", machine=machine) == "555"


def test_sbatch_dry_run_renders() -> None:
    """sbatch(dry_run=True) renders the full command string."""
    rendered = sbatch(script="/x.sh", gpus=2, dry_run=True)
    assert rendered.startswith("sbatch --gpus=2") and rendered.endswith("/x.sh")


def test_scancel_runs_and_dry_run() -> None:
    """scancel runs the cancel on the machine, and renders under dry_run."""
    machine = machine_with("")
    scancel(["1", "2"], machine=machine)
    assert machine.calls[0][:1] == ["scancel"]
    assert scancel("3", dry_run=True) == "scancel 3"


def test_qstat_runs_parsed_unparsed_and_full() -> None:
    """qstat runs and parses the standard table; full_output parses -f; unparsed returns raw."""
    table = "Job ID   Name  User  Time Use S Queue\n--- \n1.s job u 00:01:00 R q\n"
    [job] = qstat(machine=machine_with(table))
    assert isinstance(job, JobInfo) and job.job_id == "1.s"

    full = "Job Id: 9.s\n    job_state = F\n"
    [fjob] = qstat(full_output=True, machine=machine_with(full))
    assert fjob.job_id == "9.s"

    assert qstat(machine=machine_with("raw\n"), parse_output=False) == "raw\n"


def test_qstat_flag_combinations_dry_run() -> None:
    """qstat threads -a (all), -t (arrays), -u (user); -a suppresses -f."""
    assert qstat(all_jobs=True, show_arrays=True, user="me", dry_run=True) == "qstat -a -t -u me"


def test_qdel_runs() -> None:
    """qdel runs the delete on the machine (the non-dry-run path)."""
    machine = machine_with("")
    qdel("1.s", machine=machine)
    assert machine.calls[0] == ["qdel", "1.s"]


def test_qsub_runs_and_extracts_id() -> None:
    """qsub runs on the machine and extracts the numeric job id from stdout."""
    machine = machine_with("321.server\n")
    assert qsub(script="/x.sh", queue="q", group_list="g", select=1, machine=machine) == "321"


def test_qsub_with_stdin() -> None:
    """qsub with stdin feeds the script body via `<<` before running."""
    machine = machine_with("99.s\n")
    assert qsub(queue="q", group_list="g", select=1, stdin="echo hi", machine=machine) == "99"


def test_qstat_full_record_extra_fields() -> None:
    """qstat -f folds server/project/egroup/output+error paths/comment and resources sub-dicts."""
    record = (
        "Job Id: 9.s\n"
        "    job_state = R\n"
        "    server = pbsserver\n"
        "    project = proj\n"
        "    egroup = grp\n"
        "    Output_Path = host:/logs/o\n"
        "    Error_Path = host:/logs/e\n"
        "    comment = waiting\n"
        "    Resource_List.ncpus = 8\n"
        "    resources_used.cput = 00:30:00\n"
    )
    [job] = qstat(full_output=True, machine=machine_with(record))
    assert job.server == "pbsserver"
    assert job.project == "proj"
    assert job.group == "grp"
    assert str(job.output_path) == "host:/logs/o"
    assert str(job.error_path) == "host:/logs/e"
    assert job.comment == "waiting"
    assert job.resources_requested["ncpus"] == "8"
    assert job.resources_used["cput"] == "00:30:00"


def test_qstat_standard_short_rows_dropped() -> None:
    """A standard-layout body row with fewer than five fields is skipped, not crashed on."""
    table = "Job ID Name User Time S Queue\n--- \n1.s only two\n"
    assert qstat(machine=machine_with(table)) == []


def test_qstat_standard_five_field_row_has_no_walltime() -> None:
    """A five-field standard row (no walltime-used column) parses with walltime_used None."""
    table = "Job ID Name User S Queue\n--- \n1.s job alice R gpu\n"
    [job] = qstat(machine=machine_with(table))
    assert job.job_id == "1.s" and job.walltime_used is None


def test_qstat_wide_short_row_dropped() -> None:
    """A wide-vendor row with fewer than eight fields is skipped."""
    table = "JOB_ID JOB_NAME STATUS PROJECT QUEUE\n1 train RUNNING proj gpu\n"
    assert qstat(machine=machine_with(table)) == []


def test_qstat_full_ignores_lines_without_equals() -> None:
    """A `qstat -f` continuation line lacking ` = ` is ignored, not parsed as a field."""
    record = "Job Id: 9.s\n    job_state = R\n    a-bare-line-no-equals\n"
    [job] = qstat(full_output=True, machine=machine_with(record))
    assert job.job_id == "9.s" and job.state is JobState.RUNNING


def test_sacct_and_squeue_skip_blank_lines() -> None:
    """Blank lines in sacct/squeue output are skipped before splitting."""
    assert sacct("7", machine=machine_with("\n7|COMPLETED|0:0\n")).state is SlurmState.COMPLETED
    assert squeue(machine=machine_with("\n1|n|RUNNING|gpu|00:01:00\n"))[0].job_id == "1"


def test_sbatch_mem_gb_and_account_flags() -> None:
    """sbatch threads mem_gb into `--mem=<n>G` and account into `--account=` (the runner path)."""
    machine = machine_with("Submitted batch job 1\n")
    sbatch(script="/x.sh", mem_gb=128, account="proj", machine=machine)
    assert "--mem=128G" in machine.calls[0]
    assert "--account=proj" in machine.calls[0]


def test_qsub_extra_resources_stderr_and_interactive() -> None:
    """build extras: extra_resources -> -l k=v, stderr_path -> -e, interactive -> -I."""
    machine = machine_with("5.s\n")
    qsub(
        script="/x.sh",
        queue="q",
        group_list="g",
        select=1,
        stderr_path="/logs/e",
        interactive=True,
        extra_resources={"ngpus": "2"},
        machine=machine,
    )
    [call] = machine.calls
    assert "ngpus=2" in call
    assert call[call.index("-e") + 1] == "/logs/e"
    assert "-I" in call


def test_resource_spec_mpiprocs_and_ompthreads() -> None:
    """to_select_clause includes mpiprocs/ompthreads when set."""
    spec = ResourceSpec(select=1, mpiprocs=4, ompthreads=2)
    assert spec.to_select_clause() == "select=1:mpiprocs=4:ompthreads=2"


def test_resource_spec_host_vnode_software_clauses() -> None:
    """extra_clauses emits the host/vnode/software fields, and an empty select clause is blank."""
    spec = ResourceSpec(host="n1", vnode="v1", software="lic")
    assert spec.extra_clauses() == ["host=n1", "vnode=v1", "software=lic"]
    assert ResourceSpec().to_select_clause() == ""


def test_sacct_and_squeue_skip_short_rows() -> None:
    """sacct returns None for all-short output; squeue drops short rows; sbatch empty -> empty."""
    assert sacct("7", machine=machine_with("7|x\n")) is None  # < 3 fields -> no match
    assert squeue(machine=machine_with("only|two\n")) == []
    assert sbatch(script="/x.sh", machine=machine_with("")) == ""


# --- pueue client runners ---


def test_pueue_add_threads_all_options() -> None:
    """pueue.add threads label/group/after/immediate/working_directory into argv + returns id."""
    machine = machine_with("42\n")
    out = pueue.add(
        "run x",
        machine=machine,
        label="lbl",
        group="grp",
        after=[1, 2],
        immediate=True,
        working_directory="/repo",
    )
    assert out == "42"
    [call] = machine.calls
    assert call[:2] == ["pueue", "add"]
    assert "--label" in call and "lbl" in call
    assert "--group" in call and "grp" in call
    assert call.count("--after") == 2
    assert "--immediate" in call
    assert "--working-directory" in call and "/repo" in call
    assert call[-1] == "run x"


def test_pueue_add_minimal_omits_optional_flags() -> None:
    """With no label/group/deps/working-dir, add emits only `add --print-task-id -- <cmd>`."""
    machine = machine_with("1\n")
    pueue.add("run x", machine=machine)
    [call] = machine.calls
    assert "--label" not in call and "--working-directory" not in call
    assert call[-2:] == ["--", "run x"]


def test_pueue_status_parses_failed_and_success() -> None:
    """status folds externally-tagged results: a Failed dict -> exit code, Success -> 0."""
    snapshot = {
        "tasks": {
            "0": {"id": 1, "label": "a", "status": {"Done": {"result": "Success", "start": "t"}}},
            "1": {"id": 2, "label": "b", "status": {"Done": {"result": {"Failed": 7}}}},
        }
    }
    tasks = pueue.status(machine=machine_with(json.dumps(snapshot)))
    by_id = {t.id: t for t in tasks}
    assert by_id[1].exit_code == 0 and by_id[1].result == "Success"
    assert by_id[2].exit_code == 7 and by_id[2].result == "Failed"


def test_pueue_log_and_kill_and_clean() -> None:
    """log tails the captured output, kill takes one or many ids, clean drops finished tasks."""
    assert pueue.log(7, machine=machine_with("body\n"), lines=10) == "body\n"
    machine = machine_with("", "", "")
    pueue.log(7, machine=machine)  # the --full branch
    pueue.kill([1, 2], machine=machine)
    pueue.clean(machine=machine, successful_only=True)
    assert machine.calls[0][:2] == ["pueue", "log"]
    assert machine.calls[1][:2] == ["pueue", "kill"]
    assert machine.calls[2][:2] == ["pueue", "clean"]


# --- rsync command runner (the run=True path) ---


def test_rsync_runs_via_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """rsync(run=True) executes the built command through plumbum local and returns its stdout."""
    import fleet.clients.rsync.command as cmd_mod

    class FakeCmd:
        def __getitem__(self, _args: object) -> FakeCmd:
            return self

        def __call__(self) -> str:
            return "sent\n"

        def __str__(self) -> str:
            return "rsync ..."

    monkeypatch.setitem(cmd_mod.__dict__, "local", {"rsync": FakeCmd()})
    out = rsync(
        "a", "b", Rsync.ARCHIVE, include=["keep/"], timeout=30, extra=["--itemize-changes"]
    )
    assert out == "sent\n"


# --- scheduler runners: status / logs / cancel over each backend ---


def test_pbs_status_logs_cancel(remote: RecordingMachine) -> None:
    """Pbs status/logs/cancel each run a login-shell `bash -lc` carrying the right inner verb."""
    Pbs().status(remote, "/repo")
    Pbs().logs(remote, "/repo", "7", follow=True)
    Pbs().cancel(remote, "/repo", "7")
    inners = [c[2] for c in remote.calls]
    assert any("status" in i for i in inners)
    assert any("logs 7 --follow" in i for i in inners)
    assert any("cancel 7" in i for i in inners)


def test_slurm_status_logs_cancel(remote: RecordingMachine) -> None:
    """Slurm status/logs/cancel route through the on-host login shell like PBS."""
    Slurm().status(remote, "/repo")
    Slurm().logs(remote, "/repo", "7", follow=False)
    Slurm().cancel(remote, "/repo", "7")
    inners = [c[2] for c in remote.calls]
    assert any("status" in i for i in inners)
    assert any("logs 7" in i for i in inners)
    assert any("cancel 7" in i for i in inners)


def test_pueue_status_logs_cancel(remote: RecordingMachine) -> None:
    """Pueue status renders tasks, logs prints the captured log, cancel kills the task."""
    empty = json.dumps({"tasks": {}})
    remote.outputs = [empty, "logbody\n", ""]
    Pueue().status(remote, "/repo")  # empty snapshot -> renders "(no tasks)"
    Pueue().logs(remote, "/repo", "7", follow=False)
    Pueue().cancel(remote, "/repo", "7")
    cmds = [c[0] for c in remote.calls]
    assert cmds.count("pueue") == 3


def test_pueue_logs_follow(remote: RecordingMachine) -> None:
    """Pueue.logs with follow streams `pueue follow <handle>` via FG instead of printing."""
    Pueue().logs(remote, "/repo", "7", follow=True)
    [call] = remote.calls
    assert call == ["pueue", "follow", "7"]


def test_local_status_and_cancel_are_noops(remote: RecordingMachine) -> None:
    """Local backend has no queue: status and cancel only log, issuing no command."""
    Local().status(remote, "/repo")
    Local().cancel(remote, "/repo", "7")
    assert remote.calls == []


def test_local_logs_runs_login_shell(remote: RecordingMachine) -> None:
    """Local.logs runs the on-host `logs <handle>` through the login shell."""
    Local().logs(remote, "/repo", "7", follow=True)
    assert "logs 7 --follow" in remote.calls[0][2]


def test_pbs_and_slurm_submit_empty_output(remote: RecordingMachine) -> None:
    """An empty submit stdout yields an empty handle rather than an index error."""
    remote.outputs = [""]
    assert Pbs().submit(remote, "/repo", "x.sh", [], resources=Resources()) == ""
    remote.outputs = [""]
    assert Slurm().submit(remote, "/repo", "x.sh", [], resources=Resources()) == ""


# --- executor table renderers + experiments_root ---


def recording_console() -> Console:
    """A Console that records its output so a table render can be inspected as text."""
    return Console(width=80, record=True, force_terminal=False, color_system=None)


def test_print_jobs_table_smoke() -> None:
    """The PBS table renderer prints a row per job (state palette + fallbacks for unknown/None)."""
    console = recording_console()
    jobs = [
        JobInfo(
            job_id="1.s",
            name="a",
            user="u",
            state=JobState.RUNNING,
            queue="q",
            walltime="01:00:00",
        ),
        JobInfo(job_id="2.s", name="b", user="u", state="X", queue=""),
    ]
    _print_jobs_table(jobs, console=console)
    out = console.export_text()
    assert "a" in out and "b" in out


def test_print_slurm_table_smoke() -> None:
    """The SLURM table renderer prints rows for known and unknown states."""
    console = recording_console()
    jobs = [
        SlurmJob(
            job_id="1", name="a", state=SlurmState.RUNNING, partition="gpu", elapsed="00:01:00"
        ),
        SlurmJob(job_id="2", name="b", state="WEIRD"),
    ]
    _print_slurm_table(jobs, console=console)
    out = console.export_text()
    assert "RUNNING" in out and "WEIRD" in out


def test_experiments_root_finds_pixi_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """experiments_root walks up to the dir holding pixi.toml and returns its research/ tree."""
    (tmp_path / "pixi.toml").write_text("")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert experiments_root() == tmp_path / "research"


def test_experiments_root_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no pixi.toml above, experiments_root falls back to the CWD."""
    monkeypatch.chdir(tmp_path)
    assert experiments_root() == Path.cwd()


# --- executor/local.py job-local scratch ---


def test_job_local_root_prefers_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_job_local_root prefers an existing LOCALDIR, then TMPDIR, else /tmp."""
    monkeypatch.delenv("LOCALDIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert get_job_local_root() == tmp_path


def test_job_local_root_defaults_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no usable env var set, get_job_local_root falls back to /tmp."""
    monkeypatch.delenv("LOCALDIR", raising=False)
    monkeypatch.setenv("TMPDIR", "/does/not/exist/xyz")
    assert get_job_local_root() == Path("/tmp")


def test_ensure_job_local_root_creates_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_job_local_root makes and returns a directory under the resolved root."""
    monkeypatch.delenv("LOCALDIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    created = ensure_job_local_root("job", "scratch")
    assert created == tmp_path / "job" / "scratch" and created.is_dir()
