from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import lote.cli as cli
from lote.cache import RunRecord
from lote.cli import Lote, _split_targets, git, recorded, row, run_tty
from lote.executor.cli import handled
from lote.jobspec import JobSpec
from lote.models import Target
from lote.schedulers import JobState

from .conftest import GB10, FakeRemote, RecordingScheduler


def make_run(
    handle: str,
    *,
    target: str,
    script: str = "a.sh",
    submitted_at: str = "t0",
    fetch_path: str | None = None,
) -> RunRecord:
    """A fully-populated :class:`RunRecord` for CLI tests (only the varied fields are args)."""
    return RunRecord(
        handle=handle,
        target=target,
        kind="ssh",
        script=script,
        args="",
        git_sha="abc1234",
        dirty=0,
        submitted_at=submitted_at,
        fetch_path=fetch_path,
    )


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> RecordingScheduler:
    """Patch the CLI's seams: pick -> a recording backend, connect -> a fake remote, git fixed."""
    sched = RecordingScheduler()
    monkeypatch.setattr(cli, "pick", lambda _machine: sched)
    monkeypatch.setattr(cli, "connect", lambda _name: FakeRemote())
    monkeypatch.setattr(cli, "git", lambda *a: "abc1234" if a[0] == "rev-parse" else "")
    return sched


@pytest.fixture
def lote(workdir: Path) -> Lote:
    """A Lote whose `.lote/` writes land in the isolated workdir."""
    return Lote()


def seed_target(lote: Lote, monkeypatch: pytest.MonkeyPatch, target: Target = GB10) -> Target:
    """Make `_target(alias)` resolve to `target` without onboarding/probing."""
    monkeypatch.setattr(Lote, "_target", lambda self, alias: target)
    return target


# --- construction & laziness ---


def test_state_properties_are_lazy_cached(lote: Lote) -> None:
    """`_config`/`_cache`/`_history` are cached_propertys: built on first touch, then identical."""
    assert "_cache" not in lote.__dict__  # untouched: not yet built
    cache_first = lote._cache
    assert lote._cache is cache_first  # cached
    assert lote._config is lote._config
    assert lote._history is lote._history


def test_history_property_attaches_file_sink_only_when_enabled(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_history` wires the rotating file sink when history is on, and skips it when opted out."""
    added: list[object] = []
    monkeypatch.setattr(cli.logger, "add", lambda *a, **k: added.append(a))

    monkeypatch.setenv("LOTE_NO_HISTORY", "1")
    assert lote._history.enabled is False
    assert added == []  # disabled: no file sink attached

    fresh = Lote()
    monkeypatch.delenv("LOTE_NO_HISTORY", raising=False)
    assert fresh._history.enabled is True
    assert len(added) == 1  # enabled: the rotating log sink is added once


# --- recorded decorator ---


def test_recorded_records_ok_and_reraises_error(lote: Lote) -> None:
    """`@recorded` appends an `ok` event on return and an `error` event (then reraises)."""

    class Probe:
        _history = lote._history

        @recorded
        def good(self) -> str:
            return "HANDLE"

        @recorded
        def bad(self) -> None:
            raise SystemExit("boom")

    probe = Probe()
    assert probe.good() == "HANDLE"
    with pytest.raises(SystemExit):
        probe.bad()
    events = lote._history.recent(10)
    outcomes = {e.command: e.outcome for e in events}
    assert outcomes["good"] == "ok"
    assert outcomes["bad"] == "error"
    handles = {e.command: e.handle for e in events}
    assert handles["good"] == "HANDLE"  # str result recorded as the handle


# --- pure helpers ---


def test_row_from_jobstate() -> None:
    """`row` projects a JobState into a ReconcileRow, carrying script/submitted_at."""
    state = JobState(handle="7", state="F", exit_code=0, verdict="ok")
    r = row(state, script="x.sh", submitted_at="t0")
    assert (r.handle, r.script, r.submitted_at, r.state, r.exit_code, r.verdict) == (
        "7",
        "x.sh",
        "t0",
        "F",
        0,
        "ok",
    )


def test_run_tty_dry_run_prints(capsys: pytest.CaptureFixture[str]) -> None:
    """run_tty under dry_run prints the shell-joined command and runs nothing."""
    run_tty(["ssh", "-t", "host"], dry_run=True)
    assert capsys.readouterr().out.strip() == "ssh -t host"


def test_run_tty_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without dry_run, run_tty shells out via subprocess.run."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, *, check: captured.update(cmd=cmd))
    run_tty(["ssh", "host"], dry_run=False)
    assert captured["cmd"] == ["ssh", "host"]


def test_git_strips_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """git() returns the stripped stdout of the local git call."""
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " abc \n"})
    )
    assert git("rev-parse", "HEAD") == "abc"


def test_split_targets_accepts_commas_and_whitespace() -> None:
    """`_split_targets` parses commas or spaces and drops empty fragments (trailing comma ok)."""
    assert _split_targets("gold, miyabi") == ["gold", "miyabi"]
    assert _split_targets("gold miyabi") == ["gold", "miyabi"]
    assert _split_targets("gold,") == ["gold"]


# --- ls / discover / setup ---


def test_ls_renders_cached_targets(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """ls lists each target alias with its cached facts (None when never probed); never probes."""
    monkeypatch.setattr(Lote, "_targets", lambda self: ["spark", "ghost"])
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: GB10 if alias == "spark" else None)
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "targets", lambda rows: rendered.append(rows))
    lote.ls()
    assert rendered == [[("spark", GB10), ("ghost", None)]]


def test_probe_previews_without_syncing_or_caching(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """probe renders the resolved row from a live over-ssh read, without onboarding or caching."""
    monkeypatch.setattr(cli, "connect", lambda _name: FakeRemote())
    monkeypatch.setattr(cli, "probe_capabilities", lambda remote, alias: GB10)
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "targets", lambda rows: rendered.append(rows))

    lote.probe("spark")

    assert rendered == [[("spark", GB10)]]
    assert lote._cache.facts("spark") is None  # a preview caches nothing


def test_discover_onboards_and_renders(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """discover onboards the target and renders the single resolved row."""
    monkeypatch.setattr(Lote, "_onboard", lambda self, alias: GB10)
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "targets", lambda rows: rendered.append(rows))
    lote.discover("spark")
    assert rendered == [[("spark", GB10)]]


def test_setup_onboards(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """setup onboards the target (and logs completion)."""
    onboarded: list[str] = []
    monkeypatch.setattr(Lote, "_onboard", lambda self, alias: onboarded.append(alias) or GB10)
    lote.setup("spark")
    assert onboarded == ["spark"]


# --- submit ---


def test_submit_dispatches_and_records(
    lote: Lote,
    scheduler: RecordingScheduler,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """submit rsyncs, submits via the backend, returns the handle, and caches the run."""
    seed_target(lote, monkeypatch)
    synced: list[Target] = []
    monkeypatch.setattr(Lote, "_rsync_up", lambda self, machine, **k: synced.append(machine))

    handle = lote.submit("spark", "train.sh", "--lr", "0.1", fetch="out/")

    assert handle == "H1"
    assert capsys.readouterr().out == ""  # the CLI boundary prints the return, not the command
    assert ("submit", ("/repo", "train.sh", ("--lr", "0.1"))) in scheduler.calls
    assert synced == [GB10]
    [run] = lote._cache.recent(10)
    assert run.handle == "H1"
    assert run.target == "spark"
    assert run.script == "train.sh"
    assert run.args == "--lr 0.1"
    assert run.git_sha == "abc1234"
    assert run.fetch_path == "out/"
    assert run.dirty == 0  # git status porcelain returned ""


def test_submit_auto_requires_needs(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit auto` without --needs is a hard error before any dispatch."""
    with pytest.raises(SystemExit, match="--needs"):
        lote.submit("auto", "train.sh")


def test_submit_auto_no_fitting_target_is_a_lookup_error(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fitting target surfaces the data layer's LookupError (the boundary exits 1)."""
    monkeypatch.setattr(Lote, "_known_targets", lambda self: [])
    with pytest.raises(LookupError, match="no target fits"):
        lote.submit("auto", "train.sh", needs=4000)


def test_submit_auto_routes_by_needs(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit auto --needs N` routes to the smallest fitting known target."""
    monkeypatch.setattr(Lote, "_known_targets", lambda self: [GB10])
    monkeypatch.setattr(Lote, "_rsync_up", lambda self, machine, **k: None)
    picked: list[float] = []
    monkeypatch.setattr(cli, "smallest_fit", lambda targets, needs: picked.append(needs) or GB10)

    handle = lote.submit("auto", "train.sh", needs=40)
    assert handle == "H1"
    assert picked == [40.0]
    assert scheduler.calls[0][0] == "submit"


def test_submit_fans_across_multiple_targets(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """`submit --targets a,b` dispatches once per host and returns the joined handles.

    The positional `target` is a placeholder here; each alias goes through `_submit_one`.
    """
    dispatched: list[str] = []
    monkeypatch.setattr(
        Lote,
        "_submit_one",
        lambda self, target, script, args, **k: dispatched.append(target) or f"H-{target}",
    )
    handles = lote.submit("ignored", "train.sh", "--lr", "0.1", targets="gold, miyabi")
    assert dispatched == ["gold", "miyabi"]
    assert handles == "H-gold,H-miyabi"


# --- submit --cmd (generated job script) ---


def test_jobspec_render_pbs_vs_bash() -> None:
    """JobSpec renders a #PBS script on a PBS host and a plain bash wrapper elsewhere."""
    spec = JobSpec(cmd="python -m foo", queue="debug-g", gpus=2)
    pbs = spec.render(pbs=True)
    bash = spec.render(pbs=False)
    assert "#PBS -q debug-g" in pbs and "ngpus=2" in pbs
    assert "#PBS" not in bash
    assert "chefe run env PYTHONPATH=" in pbs and "chefe run env PYTHONPATH=" in bash


def test_write_job_script_picks_renderer_and_ships_under_lote_jobs(
    lote: Lote, workdir: Path
) -> None:
    """A generated script lands under `.lote/jobs/` and uses the host's scheduler kind."""
    pbs = Target(name="hpc", kind="pbs", root="/work")
    spec = JobSpec(cmd="python -m foo")
    path = lote._write_job_script(pbs, spec)
    assert path.startswith(".lote/jobs/") and path.endswith(".sh")
    text = Path(path).read_text()
    assert "#PBS -q debug-g" in text  # PBS host -> PBS script

    bash_path = lote._write_job_script(GB10, JobSpec(cmd="python -m foo"))
    assert "#PBS" not in Path(bash_path).read_text()  # ssh host -> bash wrapper


def test_write_job_script_is_content_addressed(lote: Lote, workdir: Path) -> None:
    """The same job text always lands on the same file, so `.lote/jobs` never balloons."""
    spec = JobSpec(cmd="python -m foo")
    first = lote._write_job_script(GB10, spec)
    second = lote._write_job_script(GB10, spec)
    assert first == second
    assert lote._write_job_script(GB10, JobSpec(cmd="python -m bar")) != first
    assert len(list((workdir / ".lote" / "jobs").iterdir())) == 2


def test_submit_cmd_generates_script_then_dispatches_it(
    lote: Lote, scheduler: RecordingScheduler, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit --cmd` writes a job script, ships it as an extra path, and submits that path."""
    pbs = Target(name="hpc", kind="pbs", root="/work")
    seed_target(lote, monkeypatch, pbs)
    shipped: list[tuple[Target, tuple[str, ...]]] = []

    def ship(self: Lote, machine: Target, *, extra: Sequence[str] = ()) -> None:
        shipped.append((machine, tuple(extra)))

    monkeypatch.setattr(Lote, "_rsync_up", ship)

    handle = lote.submit("hpc", cmd="python -m foo --model X", queue="gen-S", gpus=2)

    assert handle == "H1"
    [(_, extra)] = shipped
    [generated] = extra
    assert generated.startswith(".lote/jobs/") and Path(generated).is_file()
    # the dispatched script is exactly the generated path (no worker.sh involved).
    [(_, (_root, script, _args))] = [(k, v) for k, v in scheduler.calls if k == "submit"]
    assert script == generated
    text = Path(generated).read_text()
    assert "#PBS -q gen-S" in text and "ngpus=2" in text
    assert "chefe run env PYTHONPATH=" in text and "python -m foo --model X" in text


def test_submit_script_path_still_works_without_cmd(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: the original `submit <target> worker.sh` path ships no extra, dispatches it."""
    seed_target(lote, monkeypatch)
    shipped: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        Lote, "_rsync_up", lambda self, machine, *, extra=(): shipped.append(tuple(extra))
    )

    handle = lote.submit("spark", "worker.sh", "--lr", "0.1")

    assert handle == "H1"
    assert shipped == [()]  # no generated script shipped
    assert ("submit", ("/repo", "worker.sh", ("--lr", "0.1"))) in scheduler.calls


def test_submit_cmd_fans_across_targets_rendering_per_host(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit --cmd --targets a,b` passes the same JobSpec to each host's `_submit_one`."""
    specs: list[object] = []
    monkeypatch.setattr(
        Lote,
        "_submit_one",
        lambda self, target, script, args, *, spec, **k: specs.append(spec) or f"H-{target}",
    )
    handles = lote.submit("ignored", cmd="python -m foo", targets="gold, miyabi")
    assert handles == "H-gold,H-miyabi"
    assert all(isinstance(s, JobSpec) and s.cmd == "python -m foo" for s in specs)


# --- ps / status / reconcile ---


def test_ps_no_target_renders_recent_runs(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ps` with no target hands the cache's recent runs to the renderer (the cross-host view)."""
    monkeypatch.setattr(lote._cache, "recent", lambda limit: [{"handle": "H1"}])
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "runs", lambda runs: rendered.append(runs))
    lote.ps(limit=5)
    assert rendered == [[{"handle": "H1"}]]


def test_ps_target_lists_live_scheduler_jobs(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ps <target>` asks the host's backend for its live jobs and renders them uniformly."""
    seed_target(lote, monkeypatch)
    rendered: list[tuple[str, object]] = []
    monkeypatch.setattr(
        lote._render, "states", lambda target, states: rendered.append((target, states))
    )
    lote.ps("spark")
    assert ("jobs", ("/repo",)) in scheduler.calls
    [(target, states)] = rendered
    assert target == "spark" and [s.handle for s in states] == ["H1"]


def test_status_delegates_to_backend(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status opens a connection and calls the backend's status with the repo root."""
    seed_target(lote, monkeypatch)
    lote.status("spark")
    assert ("status", ("/repo",)) in scheduler.calls


def test_reconcile_compares_cached_runs(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reconcile pulls this target's cached runs, asks the backend per state, and renders rows."""
    seed_target(lote, monkeypatch)
    runs = [
        make_run("H1", target="spark", script="a.sh", submitted_at="t0"),
        make_run("H2", target="other", script="b.sh", submitted_at="t1"),
    ]
    monkeypatch.setattr(lote._cache, "recent", lambda limit: runs)
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "reconcile", lambda rows: rendered.append(rows))

    lote.reconcile("spark")
    state_calls = [c for c in scheduler.calls if c[0] == "state"]
    assert state_calls == [("state", ("/repo", "H1"))]  # only the spark-owned run
    [rows] = rendered
    assert [r.handle for r in rows] == ["H1"]


# --- interact ---


def test_interact_ssh_opens_login_shell(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-PBS target opens `ssh -t <name>` (dry-run prints it)."""
    seed_target(lote, monkeypatch)
    captured: list[list[str]] = []
    monkeypatch.setattr(cli, "run_tty", lambda cmd, dry: captured.append(cmd))
    lote.interact("spark", dry_run=True)
    assert captured == [["ssh", "-t", "spark"]]


def test_interact_pbs_builds_qsub_interactive(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PBS target builds an interactive `qsub -I` carrying queue and group_list."""
    pbs = Target(name="hpc", kind="pbs", root="/work", queue="interactive", account="grp")
    seed_target(lote, monkeypatch, pbs)
    captured: list[list[str]] = []
    monkeypatch.setattr(cli, "run_tty", lambda cmd, dry: captured.append(cmd))
    lote.interact("hpc", gpus=2, hours=4, dry_run=True)
    [cmd] = captured
    assert cmd[:3] == ["ssh", "-t", "hpc"]
    inner = cmd[3]
    assert "qsub -I" in inner
    assert "select=2" in inner
    assert "walltime=04:00:00" in inner
    assert "interactive" in inner
    assert "group_list=grp" in inner


def test_interact_pbs_derives_group_from_work_root(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no account, the group_list comes from the project group in a /work/<group> root."""
    pbs = Target(name="hpc", kind="pbs", root="/work/xg25g007/me/projects")
    seed_target(lote, monkeypatch, pbs)
    captured: list[list[str]] = []
    monkeypatch.setattr(cli, "run_tty", lambda cmd, dry: captured.append(cmd))
    lote.interact("hpc", dry_run=True)
    assert "group_list=xg25g007" in captured[0][3]


def test_interact_pbs_without_queue_or_account(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PBS target with no discovered queue/account omits the -q and -W group_list flags."""
    pbs = Target(name="hpc", kind="pbs", root="/work")
    seed_target(lote, monkeypatch, pbs)
    captured: list[list[str]] = []
    monkeypatch.setattr(cli, "run_tty", lambda cmd, dry: captured.append(cmd))
    lote.interact("hpc", dry_run=True)
    inner = captured[0][3]
    assert "-q" not in inner.split()
    assert "group_list" not in inner


# --- run (through the scheduler) ---


def test_run_without_command_opens_shell(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run` with neither command nor file delegates to the interactive shell.

    No queue is forced on the shell, so a PBS target keeps its probed queue
    instead of the batch-only `debug-g` default; an explicit --queue still wins.
    """
    seed_target(lote, monkeypatch)
    opened: list[tuple[Target, str]] = []
    monkeypatch.setattr(
        Lote, "_shell", lambda self, machine, *, queue, **k: opened.append((machine, queue))
    )
    assert lote.run("spark") is None
    assert lote.run("spark", queue="interactive") is None
    assert opened == [(GB10, ""), (GB10, "interactive")]


def test_run_dispatches_through_scheduler_then_streams(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run <target> "<cmd>"` submits a generated job through the queue, then streams it.

    The command is wrapped in a JobSpec and dispatched via `_submit_one` (so it is
    queued, captured, and cancellable), and the non-detached path streams the log and
    propagates the job's exit code.
    """
    seed_target(lote, monkeypatch)
    submitted: list[JobSpec | None] = []
    monkeypatch.setattr(
        Lote,
        "_submit_one",
        lambda self, target, script, args, *, spec, **k: submitted.append(spec) or "H7",
    )
    streamed: list[tuple[Target, str]] = []
    monkeypatch.setattr(
        Lote, "_stream", lambda self, machine, handle: streamed.append((machine, handle))
    )
    assert lote.run("spark", "nvidia-smi", gpus=2, account="xg25g007") is None
    [spec] = submitted
    assert isinstance(spec, JobSpec) and spec.cmd == "nvidia-smi" and spec.gpus == 2
    assert spec.queue == "debug-g"  # the batch default applies only to generated scripts
    assert spec.account == "xg25g007"  # threaded into the generated #PBS -W group_list
    assert streamed == [(GB10, "H7")]


def test_run_detach_returns_handle_without_streaming(
    lote: Lote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`run --detach` returns the handle and never streams, leaving the job running."""
    seed_target(lote, monkeypatch)
    monkeypatch.setattr(Lote, "_submit_one", lambda self, *a, **k: "H9")
    streamed: list[object] = []
    monkeypatch.setattr(Lote, "_stream", lambda self, machine, handle: streamed.append(handle))
    handle = lote.run("spark", "train.py", detach=True)
    assert handle == "H9"
    assert streamed == []  # detached: no follow
    assert capsys.readouterr().out == ""  # the CLI boundary prints the return, not the command


def test_run_ships_file_before_dispatching(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `--file` is scp'd under `.lote/run-` and dispatched as `python <remote file>`."""
    seed_target(lote, monkeypatch)
    runs: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess, "run", lambda cmd, **k: runs.append(cmd) or SimpleNamespace(returncode=0)
    )
    specs: list[JobSpec] = []
    monkeypatch.setattr(
        Lote,
        "_submit_one",
        lambda self, target, script, args, *, spec, **k: specs.append(spec) or "H1",
    )
    monkeypatch.setattr(Lote, "_stream", lambda self, machine, handle: None)
    lote.run("spark", file="train.py")
    scp = runs[0]
    assert scp[0] == "scp" and scp[1] == "train.py"
    assert scp[2] == "spark:/repo/.lote/run-train.py"
    assert specs[0].cmd == "python .lote/run-train.py"


def test_stream_relays_log_until_terminal(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_stream` delegates to the backend's stream and returns quietly on an ok verdict."""
    assert lote._stream(GB10, "H1") is None
    assert scheduler.calls == [("stream", ("/repo", "H1"))]


def test_stream_nonzero_exit_raises_systemexit(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that ends non-zero makes `_stream` raise SystemExit with that code."""
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=5, verdict="failed")
    with pytest.raises(SystemExit) as excinfo:
        lote._stream(GB10, "H1")
    assert excinfo.value.code == 5


def test_stream_failed_without_exit_code_still_exits_nonzero(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed/vanished job with no recorded exit code exits 1, never a false success."""
    scheduler.state_result = JobState(handle="H1", state=None, exit_code=None, verdict="vanished")
    with pytest.raises(SystemExit) as excinfo:
        lote._stream(GB10, "H1")
    assert excinfo.value.code == 1


# --- status (no-target aggregation) ---


def test_status_aggregates_across_targets(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Argument-free status walks every onboarded host and renders one unified jobs table.

    spark is cached with a run, ghost is uncached, idle is cached with no runs.
    """
    monkeypatch.setattr(Lote, "_targets", lambda self: ["spark", "ghost", "idle"])
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: None if alias == "ghost" else GB10)
    run = make_run("H1", target="spark", script="a.sh", submitted_at="t0")
    monkeypatch.setattr(lote._cache, "recent", lambda limit: [run])
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "jobs", lambda rows: rendered.append(rows))
    lote.status()
    [rows] = rendered
    assert [(alias, r.handle) for alias, r in rows] == [("spark", "H1")]


# --- monitor (live multi-host loop) ---


class FakeLive:
    """A rich.Live stand-in: a context manager that records each `update(...)` renderable."""

    def __init__(self, updates: list[object]) -> None:
        self.updates = updates

    def __enter__(self) -> FakeLive:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def update(self, renderable: object) -> None:
        self.updates.append(renderable)


def test_monitor_loops_fetches_and_renders_then_stops_on_ctrl_c(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """monitor ticks: job rows + fetched parquet progress feed the live view; ctrl-c exits."""
    monkeypatch.setattr(Lote, "_job_rows", lambda self, aliases: [(aliases[0], "ROW")])
    fetched: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        Lote, "_fetch_progress", lambda self, aliases, path: fetched.append((aliases, path)) or 5
    )
    updates: list[object] = []
    monkeypatch.setattr(lote._render, "live", lambda: FakeLive(updates))
    monkeypatch.setattr(
        lote._render, "monitor", lambda jobs, progress, *, path: ("frame", jobs, progress, path)
    )
    # the loop ticks once, then ctrl-c on the sleep ends it without raising.
    monkeypatch.setattr(cli, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))

    lote.monitor("gold", "miyabi", interval=0.0, fetch="out/")

    assert fetched == [(["gold", "miyabi"], "out/")]
    assert updates == [("frame", [("gold", "ROW")], 5, "out/")]


def test_monitor_without_fetch_skips_progress_and_defaults_targets(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no targets monitor walks every host; with no fetch path progress is None."""
    monkeypatch.setattr(Lote, "_targets", lambda self: ["a", "b"])
    seen: list[list[str]] = []
    monkeypatch.setattr(Lote, "_job_rows", lambda self, aliases: seen.append(aliases) or [])
    progress_calls: list[object] = []
    monkeypatch.setattr(
        Lote, "_fetch_progress", lambda self, aliases, path: progress_calls.append(path) or 0
    )
    updates: list[object] = []
    monkeypatch.setattr(lote._render, "live", lambda: FakeLive(updates))
    monkeypatch.setattr(
        lote._render, "monitor", lambda jobs, progress, *, path: ("frame", progress)
    )
    monkeypatch.setattr(cli, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))

    lote.monitor()

    assert seen == [["a", "b"]]  # defaulted to every onboarded host
    assert progress_calls == []  # no fetch path: progress never computed
    assert updates == [("frame", None)]


def test_fetch_progress_fetches_each_then_counts_parquet_parts(
    lote: Lote, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_fetch_progress rsyncs each alias, then tallies part-*.parquet under the merged dir."""
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: GB10)
    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(Lote, "_fetch", lambda self, target, path: fetched.append((target, path)))
    out = workdir / "out" / "device=0"
    out.mkdir(parents=True)
    (out / "part-host-1.parquet").write_text("")
    (out / "part-host-2.parquet").write_text("")
    (out / "_ledger.json").write_text("")  # non-part files are ignored

    count = lote._fetch_progress(["gold", "miyabi"], "out")

    assert fetched == [("gold", "out"), ("miyabi", "out")]
    assert count == 2


def test_fetch_progress_skips_unonboarded_aliases(
    lote: Lote, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alias without cached facts is skipped, never onboarded mid-monitor-loop."""
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: GB10 if alias == "gold" else None)
    fetched: list[str] = []
    monkeypatch.setattr(Lote, "_fetch", lambda self, target, path: fetched.append(target))
    (workdir / "out").mkdir()

    assert lote._fetch_progress(["gold", "ghost"], "out") == 0
    assert fetched == ["gold"]


def test_fetch_progress_tolerates_missing_remote_path(
    lote: Lote, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host whose results path does not exist yet counts as nothing, not a crash."""
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: GB10)
    fetched: list[str] = []

    def fetch(self: Lote, target: str, path: str) -> None:
        if target == "cold":
            raise cli.ProcessExecutionError(["rsync"], 23, "", "no such file")
        fetched.append(target)

    monkeypatch.setattr(Lote, "_fetch", fetch)
    out = workdir / "out"
    out.mkdir()
    (out / "part-gold-1.parquet").write_text("")

    assert lote._fetch_progress(["cold", "gold"], "out") == 1
    assert fetched == ["gold"]  # the cold host is skipped for this tick only


# --- logs / info ---


def test_logs_prints_captured_log(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """logs without --follow prints the captured log once via the backend."""
    seed_target(lote, monkeypatch)
    lote.logs("spark", "H1")
    assert ("logs", ("/repo", "H1")) in scheduler.calls


def test_logs_follow_streams_until_terminal(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """logs --follow streams via the backend and returns once the job is terminal."""
    seed_target(lote, monkeypatch)
    lote.logs("spark", "H1", follow=True)
    assert ("stream", ("/repo", "H1")) in scheduler.calls


def test_cancel_delegates(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel delegates to the backend's qdel/scancel/kill for the handle."""
    seed_target(lote, monkeypatch)
    lote.cancel("spark", "H1")
    assert ("cancel", ("/repo", "H1")) in scheduler.calls


def test_kill_is_cancel_alias(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kill` is a thin alias for `cancel`, stopping the handle on any backend."""
    seed_target(lote, monkeypatch)
    lote.kill("spark", "H1")
    assert ("cancel", ("/repo", "H1")) in scheduler.calls


def test_info_renders_postmortem(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """info asks the backend for one job's state and renders a single reconcile row."""
    seed_target(lote, monkeypatch)
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "reconcile", lambda rows: rendered.append(rows))
    lote.info("spark", "H1")
    assert ("state", ("/repo", "H1")) in scheduler.calls
    [rows] = rendered
    assert rows[0].handle == "H1" and rows[0].verdict == "ok"


# --- fetch / pull ---


def test_fetch_rsyncs_back(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch makes the local dir and rsyncs the remote path back into it."""
    seed_target(lote, monkeypatch)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(cli, "rsync", lambda sources, dest, *a, **k: calls.append((sources, dest)))
    lote.fetch("spark", "results")
    assert Path("results").is_dir()
    [(sources, dest)] = calls
    assert sources == ["spark:/repo/results/"]
    assert dest == "results/"


def test_pull_uses_recorded_fetch_path(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """pull looks up the run's recorded fetch_path and fetches it from the run's target."""
    seed_target(lote, monkeypatch)
    monkeypatch.setattr(
        lote._cache, "run", lambda handle: make_run("H1", target="spark", fetch_path="out/")
    )
    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(Lote, "_fetch", lambda self, target, path: fetched.append((target, path)))
    lote.pull("H1")
    assert fetched == [("spark", "out/")]


def test_pull_without_fetch_path_errors(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with no recorded fetch path is a clear SystemExit, not a silent no-op."""
    monkeypatch.setattr(
        lote._cache, "run", lambda handle: make_run("H1", target="spark", fetch_path=None)
    )
    with pytest.raises(SystemExit, match="no fetch path"):
        lote.pull("H1")


# --- history ---


def test_history_renders_recent(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """history hands the recent events to the renderer."""
    monkeypatch.setattr(lote._history, "recent", lambda limit: ["e1", "e2"])
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "history", lambda events: rendered.append(events))
    lote.history(limit=2)
    assert rendered == [["e1", "e2"]]


# --- watch (one tick instead of the infinite loop) ---


def test_watch_resyncs_on_each_change(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """watch rsyncs once up front, then re-syncs for each batch with a non-ignored path."""
    seed_target(lote, monkeypatch)
    syncs: list[Target] = []
    monkeypatch.setattr(Lote, "_rsync_up", lambda self, machine: syncs.append(machine))
    lote.__dict__["_config"] = SimpleNamespace(sync=SimpleNamespace(include=["src/"]))
    monkeypatch.setattr(lote._sync, "ignored", lambda path: path.endswith(".pyc"))
    # one batch with a real change and an ignored one, then the iterator ends.
    monkeypatch.setattr(
        cli, "watch_files", lambda *paths: iter([{(1, "src/a.py"), (1, "src/b.pyc")}])
    )
    lote.watch("spark")
    assert len(syncs) == 2  # initial + one re-sync for the .py change


def test_watch_skips_when_only_ignored(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch with only ignored paths does not trigger a re-sync."""
    seed_target(lote, monkeypatch)
    syncs: list[Target] = []
    monkeypatch.setattr(Lote, "_rsync_up", lambda self, machine: syncs.append(machine))
    lote.__dict__["_config"] = SimpleNamespace(sync=SimpleNamespace(include=["src/"]))
    monkeypatch.setattr(lote._sync, "ignored", lambda path: True)
    monkeypatch.setattr(cli, "watch_files", lambda *paths: iter([{(1, "src/a.pyc")}]))
    lote.watch("spark")
    assert len(syncs) == 1  # only the initial sync


# --- internal target resolution ---


def test_targets_prefers_config_then_ssh(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """_targets uses lote.toml's list when set, else the ssh-config hosts."""
    lote.__dict__["_config"] = SimpleNamespace(targets=["a", "b"])
    assert lote._targets() == ["a", "b"]
    lote.__dict__["_config"] = SimpleNamespace(targets=[])
    monkeypatch.setattr(cli, "ssh_hosts", lambda: ["from-ssh"])
    assert lote._targets() == ["from-ssh"]


def test_target_falls_back_to_onboard(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """_target returns the cached resolve, else onboards the host."""
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: None)
    monkeypatch.setattr(Lote, "_onboard", lambda self, alias: GB10)
    assert lote._target("spark") is GB10


def test_cached_resolves_only_with_facts(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """_cached resolves a Target from cached facts, returning None when never probed."""
    monkeypatch.setattr(lote._cache, "facts", lambda alias: None)
    assert lote._cached("spark") is None
    monkeypatch.setattr(lote._cache, "facts", lambda alias: GB10)
    resolved = lote._cached("spark")
    assert resolved is not None and resolved.name == "spark"


def test_known_targets_keeps_onboarded(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """_known_targets drops aliases without cached facts."""
    monkeypatch.setattr(Lote, "_targets", lambda self: ["spark", "ghost"])
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: GB10 if alias == "spark" else None)
    assert lote._known_targets() == [GB10]


# --- onboard / rsync_up / connect / app (seam wiring) ---


def test_onboard_finds_root_syncs_installs_probes_caches(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_onboard finds the root, rsyncs, runs setup.sh, probes, and caches the resolved Target."""
    monkeypatch.setattr(cli, "connect", lambda _name: FakeRemote())
    monkeypatch.setattr(cli, "find_root", lambda remote: "/repo")
    monkeypatch.setattr(cli, "probe_capabilities", lambda remote, alias: GB10)
    synced: list[Target] = []
    monkeypatch.setattr(Lote, "_rsync_up", lambda self, machine, **k: synced.append(machine))
    # the bash setup runs through remote["bash"][[...]] & FG; FakeRemote needs __getitem__.
    monkeypatch.setattr(FakeRemote, "__getitem__", lambda self, _name: _Bash(), raising=False)

    machine = lote._onboard("spark")

    assert machine.name == "spark"
    assert synced and synced[0].name == "spark"
    assert lote._cache.facts("spark") == GB10  # cached only after setup succeeded


def test_setup_script_keeps_chefe_current() -> None:
    """Onboarding installs chefe from the synced source when present, so the manifest and the
    tool can never drift apart, and still upgrades from PyPI as the source-less fallback."""
    script = (Path(cli.__file__).parent / "scripts" / "setup.sh").read_text()
    assert "-e packages/chefe" in script  # source install wins when the repo carries it
    assert "--upgrade chefe" in script  # PyPI fallback keeps a source-less host current
    assert "chefe install" in script


class _Bash:
    """A minimal plumbum-command stand-in for `remote["bash"][[...]] & FG` in onboard."""

    def __getitem__(self, _args: object) -> _Bash:
        return self

    def __and__(self, _other: object) -> str:
        return ""


def test_rsync_up_builds_archive_flags(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """_rsync_up ships the include set to host:root/ with archive+compress+relative + excludes."""
    lote.__dict__["_config"] = SimpleNamespace(
        sync=SimpleNamespace(include=["src/"], exclude=["data/"])
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        cli,
        "rsync",
        lambda sources, dest, flags, *, exclude: captured.update(
            sources=sources, dest=dest, flags=flags, exclude=exclude
        ),
    )
    lote._rsync_up(GB10)
    assert captured["sources"] == ["src/"]
    assert captured["dest"] == "spark:/repo/"
    assert "data/" in captured["exclude"]


def test_rsync_up_fails_fast_on_empty_include(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """No [sync] include paths is a hard error, not an rsync no-op that 'onboards' nothing."""
    lote.__dict__["_config"] = SimpleNamespace(sync=SimpleNamespace(include=[], exclude=[]))
    ran: list[object] = []
    monkeypatch.setattr(cli, "rsync", lambda *a, **k: ran.append(a))
    with pytest.raises(SystemExit, match=r"\[sync\]"):
        lote._rsync_up(GB10)
    assert ran == []  # rsync never ran with only a destination


def test_connect_warms_master_then_inserts_cargo_bin_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect warms the ssh ControlMaster, opens an SshMachine, and prepends ~/.cargo/bin."""
    inserted: list[object] = []
    warmed: list[object] = []

    class FakeSsh:
        def __init__(self, name: str) -> None:
            self.name = name
            self.cwd = Path("/home/u")
            self.env = SimpleNamespace(path=_RecordingPath(inserted))

    monkeypatch.setattr(
        "lote.environment.subprocess.run",
        lambda argv, **_: warmed.append(argv) or SimpleNamespace(returncode=0, stderr=""),
    )
    monkeypatch.setattr("lote.environment.SshMachine", FakeSsh)
    machine = cli.connect("spark")
    assert machine.name == "spark"
    assert warmed == [["ssh", "spark", "true"]]  # master warmed before the fragile plumbum session
    assert inserted and inserted[0][0] == 0  # inserted at index 0
    assert ".cargo/bin" in str(inserted[0][1])


def test_connect_raises_clear_error_on_host_key_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host-key verification failure surfaces an actionable message, not a plumbum traceback."""
    monkeypatch.setattr(
        "lote.environment.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=255, stderr="Host key verification failed.\n"),
    )
    with pytest.raises(ConnectionError, match="host-key verification"):
        cli.connect("gold")


class _RecordingPath:
    """Records `path.insert(index, value)` calls so connect's PATH prepend is observable."""

    def __init__(self, sink: list[object]) -> None:
        self.sink = sink

    def insert(self, index: int, value: object) -> None:
        self.sink.append((index, value))


def test_app_registers_every_command_and_mounts_exec() -> None:
    """`build` wires each Lote command plus the `exec` sub-app into the cyclopts app."""
    commands = {
        "ls", "probe", "discover", "setup", "submit", "run", "ps", "status", "monitor",
        "reconcile", "interact", "logs", "cancel", "kill", "info", "fetch", "pull",
        "watch", "history", "exec",
    }  # fmt: skip
    assert commands <= set(cli.app)
    assert {"run", "qsub", "sbatch", "status", "info", "logs", "cancel"} <= set(cli.app["exec"])


def test_handled_prints_returned_value_once(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI boundary prints a command's returned handle exactly once, returning None."""
    assert handled(lambda: "H1")() is None
    assert capsys.readouterr().out == "H1\n"
    assert handled(lambda: None)() is None
    assert capsys.readouterr().out == ""  # a None return prints nothing


def test_handled_turns_domain_misses_into_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    """A LookupError/FileNotFoundError from the data layer becomes a one-line exit 1."""

    def missing() -> None:
        raise LookupError("no recorded run 'X'")

    with pytest.raises(SystemExit) as excinfo:
        handled(missing)()
    assert excinfo.value.code == 1
    assert "no recorded run 'X'" in capsys.readouterr().err
