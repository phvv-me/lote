import contextlib
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

import lote.cli as cli
import lote.dispatch as dispatch
from lote.cli import Lote, _split_targets, recorded, row, run_tty
from lote.dispatch import git
from lote.executor.cli import handled
from lote.jobspec import JobSpec
from lote.models import NodeClass, Target
from lote.reconcile import ReconcileRow
from lote.schedulers import DaemonDown, HostUnreachable, JobState
from lote.services import ServiceStatus

from .conftest import GB10, SNAPSHOT_LOG, FakeRemote, RecordingScheduler, make_run, make_service


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> RecordingScheduler:
    """Patch the seams: pick -> a recording backend, connect -> a fake remote, git fixed.

    The submit path now lives in `lote.dispatch` (the CLI-free core the CLI delegates to), and the
    read paths (status/poll/reconcile/...) still live in `lote.cli`, so both modules' `pick`,
    `connect`, and `git` names are pinned to the same doubles.
    """
    sched = RecordingScheduler()
    for module in (cli, dispatch):
        monkeypatch.setattr(module, "pick", lambda _machine: sched)
        monkeypatch.setattr(module, "connect", lambda _name: FakeRemote())
    monkeypatch.setattr(dispatch, "git", lambda *a: "abc1234" if a[0] == "rev-parse" else "")
    return sched


@pytest.fixture
def lote(workdir: Path) -> Lote:
    """A Lote whose `.lote/` writes land in the isolated workdir."""
    return Lote()


def seed_target(lote: Lote, monkeypatch: pytest.MonkeyPatch, target: Target = GB10) -> Target:
    """Make `target(alias)` resolve to `target` without onboarding/probing."""
    monkeypatch.setattr(Lote, "target", lambda self, alias: target)
    return target


# --- construction & laziness ---


def test_state_properties_are_lazy_cached(lote: Lote) -> None:
    """`_config`/`_cache`/`_history`/`_services` are cached_propertys: built on first touch, then
    identical."""
    assert "_cache" not in lote.__dict__  # untouched: not yet built
    cache_first = lote._cache
    assert lote._cache is cache_first  # cached
    assert lote._config is lote._config
    assert lote._history is lote._history
    assert lote._services.cache is lote._cache  # shares the CLI's cache, not a fresh one
    assert lote._services is lote._services


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
    """discover onboards the target (threading --wait) and renders the single resolved row."""
    waits: list[float] = []
    monkeypatch.setattr(Lote, "_onboard", lambda self, alias, *, wait: waits.append(wait) or GB10)
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "targets", lambda rows: rendered.append(rows))
    lote.discover("spark", wait=30.0)
    assert rendered == [[("spark", GB10)]]
    assert waits == [30.0]


def test_setup_onboards(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """setup onboards the target (and logs completion)."""
    onboarded: list[str] = []
    monkeypatch.setattr(
        Lote, "_onboard", lambda self, alias, *, wait: onboarded.append(alias) or GB10
    )
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
    monkeypatch.setattr(
        dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: synced.append(machine)
    )

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
    monkeypatch.setattr(dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: None)
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


def test_submit_cmd_threads_spec_resources_to_the_backend(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generated `--cmd` job hands its `--gpus`/`--walltime`/`--mem` to the backend as Resources.

    PBS bakes them into the `#PBS` header, but SLURM takes them as `sbatch` overrides, so the
    request must ride along as `Resources` or a `--mem`/`--gpus` submit is silently dropped there.
    """
    seed_target(lote, monkeypatch)
    monkeypatch.setattr(dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: None)
    monkeypatch.setattr(
        dispatch.Dispatcher, "write_job_script", lambda self, machine, spec: "gen.sh"
    )
    lote.submit("spark", cmd="python -m foo", gpus=2, walltime="01:00:00", mem=240)
    resources = scheduler.submit_resources
    assert resources.gpus == 2
    assert resources.walltime == "01:00:00"
    assert resources.mem_gb == 240


def test_submit_script_passes_empty_resources(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing-script submit carries no resource overrides (the script owns its directives)."""
    seed_target(lote, monkeypatch)
    monkeypatch.setattr(dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: None)
    lote.submit("spark", "train.sh")
    assert scheduler.submit_resources.gpus == 0
    assert scheduler.submit_resources.mem_gb is None


# --- submit --cmd (generated job script) ---


def test_jobspec_render_pbs_vs_bash() -> None:
    """JobSpec renders a #PBS script on a PBS host and a plain bash wrapper elsewhere."""
    spec = JobSpec(cmd="python -m foo", queue="debug-g", gpus=2)
    pbs = spec.render(pbs=True)
    bash = spec.render(pbs=False)
    assert "#PBS -q debug-g" in pbs and "ngpus=2" in pbs
    assert "#PBS" not in bash
    assert "unset PYTHONPATH" in pbs and "unset PYTHONPATH" in bash


def test_submit_cmd_generates_script_then_dispatches_it(
    lote: Lote, scheduler: RecordingScheduler, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit --cmd` writes a job script, ships it as an extra path, and submits that path."""
    pbs = Target(name="hpc", kind="pbs", root="/work")
    seed_target(lote, monkeypatch, pbs)
    shipped: list[tuple[Target, tuple[str, ...]]] = []

    def ship(self: dispatch.Dispatcher, machine: Target, *, extra: Sequence[str] = ()) -> None:
        shipped.append((machine, tuple(extra)))

    monkeypatch.setattr(dispatch.Dispatcher, "rsync_up", ship)

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
    assert "unset PYTHONPATH" in text and "python -m foo --model X" in text


def test_submit_script_path_still_works_without_cmd(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: the original `submit <target> worker.sh` path ships no extra, dispatches it."""
    seed_target(lote, monkeypatch)
    shipped: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        dispatch.Dispatcher,
        "rsync_up",
        lambda self, machine, *, extra=(): shipped.append(tuple(extra)),
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


def test_status_no_target_renders_the_resolved_jobs_table(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status` with no target renders the resolved cross-host table; `--all` widens the walk."""
    monkeypatch.setattr(Lote, "_targets", lambda self: ["spark"])
    seen: list[bool] = []

    def rows(self: Lote, aliases: list[str], *, all: bool = False) -> list[tuple[str, str]]:
        seen.append(all)
        return [(aliases[0], "ROW")]

    monkeypatch.setattr(Lote, "_job_rows", rows)
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "jobs", lambda rows, *, verbose=False: rendered.append(rows))
    lote.status()
    assert rendered == [[("spark", "ROW")]]
    assert seen == [False]  # recent-only by default
    lote.status(all=True)
    assert seen == [False, True]  # --all forwarded to the walk


def test_status_records_one_history_event_per_call(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status` is wrapped by `@recorded` exactly once, so a call logs a single history row."""
    monkeypatch.setattr(Lote, "_targets", lambda self: [])
    monkeypatch.setattr(Lote, "_job_rows", lambda self, aliases, *, all=False: [])
    monkeypatch.setattr(lote._render, "jobs", lambda rows, *, verbose=False: None)
    lote.status()
    status_events = [e for e in lote._history.recent(10) if e.command == "status"]
    assert len(status_events) == 1


def test_status_target_lists_live_scheduler_jobs(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status <target>` asks the host's backend for its live jobs and renders them uniformly."""
    seed_target(lote, monkeypatch)
    rendered: list[tuple[str, object]] = []

    def show(target: str, states: object, *, verbose: bool = False) -> None:
        rendered.append((target, states))

    monkeypatch.setattr(lote._render, "states", show)
    lote.status("spark")
    assert ("jobs", ("/repo",)) in scheduler.calls
    [(target, states)] = rendered
    assert target == "spark" and [s.handle for s in states] == ["H1"]


def test_resolve_direct_probes_a_handle_absent_from_the_batch(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending run the batched `states` misses (a finished SLURM job) gets one direct `state`.

    The batch returns nothing, so `_resolve` falls back to a single `state` probe per pending run
    and writes the freshly resolved verdict back to the cache.
    """
    monkeypatch.setattr(scheduler, "states", lambda remote, root, handles: {})
    scheduler.state_result = JobState(handle="H1", state="C", exit_code=0, verdict="ok")
    runs = [make_run("H1", target="spark")]
    rows = lote._resolve(GB10, runs)
    assert [r.verdict for r in rows] == ["ok"]
    assert ("state", ("/repo", "H1")) in scheduler.calls  # the batch miss forced a direct probe
    assert lote._cache.run("H1").verdict == "ok"  # resolved verdict written back


def test_run_row_falls_back_to_the_cached_vanished_verdict(lote: Lote) -> None:
    """A run with no live state and no cached verdict renders as `vanished`, never a crash."""
    row = lote._run_row(make_run("H1", target="spark"), live={})
    assert row.handle == "H1" and row.verdict == "vanished"


def test_resolve_renders_cached_terminals_without_probing(
    lote: Lote, scheduler: RecordingScheduler
) -> None:
    """When every run is already terminal in the cache, _resolve renders from cache and never
    touches the host, so a finished job's verdict costs no ssh round-trip."""
    done = make_run("H1", target="spark").model_copy(
        update={"verdict": "ok", "state": "F", "exit_code": 0}
    )
    rows = lote._resolve(GB10, [done])
    assert [r.verdict for r in rows] == ["ok"]
    assert scheduler.calls == []  # no states/state probe when nothing is pending


def test_resolve_marks_a_failed_ssh_probe_as_unreachable(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host whose probe raises becomes `unreachable` rows carrying the reason, not a crash."""

    def boom(remote: object, root: str, handles: list[str]) -> dict[str, JobState]:
        raise HostUnreachable("ssh channel down")

    monkeypatch.setattr(scheduler, "states", boom)
    rows = lote._resolve(GB10, [make_run("H1", target="spark")])
    assert [r.verdict for r in rows] == ["unreachable"]
    assert rows[0].state == "ssh channel down"  # the reason rides in the state cell


def test_resolve_marks_a_dead_daemon_as_unreachable_daemon_down(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A downed scheduler daemon (DaemonDown) reads as `unreachable` with reason `daemon down`."""

    def boom(remote: object, root: str, handles: list[str]) -> dict[str, JobState]:
        raise DaemonDown("daemon down")

    monkeypatch.setattr(scheduler, "states", boom)
    rows = lote._resolve(GB10, [make_run("H1", target="spark")])
    assert rows[0].verdict == "unreachable" and rows[0].state == "daemon down"


def test_resolve_keeps_cached_terminals_when_the_host_is_unreachable(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached-terminal run still renders from cache while a sibling pending run reads down."""

    def boom(remote: object, root: str, handles: list[str]) -> dict[str, JobState]:
        raise HostUnreachable("down")

    monkeypatch.setattr(scheduler, "states", boom)
    done = make_run("D1", target="spark").model_copy(update={"verdict": "ok"})
    rows = {
        r.handle: r.verdict for r in lote._resolve(GB10, [done, make_run("P1", target="spark")])
    }
    assert rows == {"D1": "ok", "P1": "unreachable"}


def test_job_rows_one_dead_host_does_not_hide_the_others(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline robustness guard: a single dead host marks only its own rows unreachable, and
    every other host still resolves and renders in the same table."""
    gold = GB10.model_copy(update={"name": "gold"})
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: GB10 if alias == "spark" else gold)
    runs = [make_run("H1", target="spark"), make_run("H2", target="gold")]
    monkeypatch.setattr(lote._cache, "recent", lambda limit: runs)

    def probe(self: Lote, cached: Target, pending: list[Any]) -> dict[str, JobState]:
        if cached.name == "spark":
            raise HostUnreachable("ssh dead")
        return {"H2": JobState(handle="H2", state="F", exit_code=0, verdict="ok")}

    monkeypatch.setattr(Lote, "_probe_host", probe)
    rows = {alias: r.verdict for alias, r in lote._job_rows(["spark", "gold"], all=True)}
    assert rows == {"spark": "unreachable", "gold": "ok"}


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


# --- poll (one-shot watcher probe) ---


@pytest.mark.parametrize(
    ("verdict", "exit_code"),
    [("ok", 0), ("failed", 1), ("running", 2), ("vanished", 3), ("unknown", 3)],
)
def test_poll_maps_each_verdict_to_an_exit_code(
    lote: Lote,
    scheduler: RecordingScheduler,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
    exit_code: int,
) -> None:
    """Each verdict exits with its agreed code so a scripted watcher branches without parsing."""
    seed_target(lote, monkeypatch)
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=None, verdict=verdict)
    with pytest.raises(SystemExit) as caught:
        lote.poll("spark", "H1")
    assert caught.value.code == exit_code
    assert scheduler.calls == [("state", ("/repo", "H1"))]  # one bounded probe, no held session


def test_poll_reports_a_queued_lifecycle_without_calling_it_running(
    lote: Lote,
    scheduler: RecordingScheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_target(lote, monkeypatch)
    scheduler.state_result = JobState(handle="H1", state="Queued", verdict="running")
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda message, *args: logged.append(str(message)))

    with pytest.raises(SystemExit) as caught:
        lote.poll("spark", "H1")

    assert caught.value.code == 2
    assert logged == ["H1 queued"]


def test_poll_persists_the_verdict_to_the_cache(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished verdict is written back so later scheduler GC cannot turn `ok` to `vanished`."""
    seed_target(lote, monkeypatch)
    lote._cache.record(make_run("H1", target="spark"))
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=0, verdict="ok")
    with pytest.raises(SystemExit):
        lote.poll("spark", "H1")
    assert lote._cache.run("H1").verdict == "ok"


def test_poll_tolerates_an_unrecorded_handle(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling a handle that was never recorded still reports the verdict (no cache write)."""
    seed_target(lote, monkeypatch)
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=0, verdict="ok")
    with pytest.raises(SystemExit) as caught:
        lote.poll("spark", "H1")
    assert caught.value.code == 0


def test_poll_exits_four_when_the_host_is_unreachable(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient connect blip exits 4 (not a false verdict); the next scheduled poll retries."""
    seed_target(lote, monkeypatch)

    def unreachable(*_: object) -> JobState:
        raise HostUnreachable("ssh channel down")

    monkeypatch.setattr(scheduler, "state", unreachable)
    with pytest.raises(SystemExit) as caught:
        lote.poll("spark", "H1")
    assert caught.value.code == 4


# --- why / wait (failure triage) ---


def test_why_surfaces_oom_or_walltime_from_exit_137_when_the_log_is_silent(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGKILLed job (exit 137, no traceback) reads as OOM/walltime, not its last good line."""
    seed_target(lote, monkeypatch)
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=137, verdict="failed")
    monkeypatch.setattr(cli, "read_log", lambda remote, root, handle: "step 400 ok\n")
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(str(msg)))
    lote.why("spark", "H1")
    [reason] = logged
    assert "memory" in reason and "walltime" in reason
    assert ("state", ("/repo", "H1")) in scheduler.calls  # the exit code came from a state probe


def test_why_returns_cleanly_on_a_failed_handle_with_a_traceback(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`why` on a failed job reads the raised exception from the log and returns without raising.

    The everyday triage case: a job that died with a Python traceback (not a silent SIGKILL).
    `why` must surface the exception's last line and complete cleanly, just as `info` and `logs`
    do on the same handle, and record one history event for the call.
    """
    seed_target(lote, monkeypatch)
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=1, verdict="failed")
    log = "loading shards\nTraceback (most recent call last):\nValueError: bad config\n"
    monkeypatch.setattr(cli, "read_log", lambda remote, root, handle: log)
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(str(msg)))
    assert lote.why("spark", "H1") is None
    assert logged == ["ValueError: bad config"]
    why_events = [e for e in lote._history.recent(10) if e.command == "why"]
    assert len(why_events) == 1  # the triage verb is audited like its siblings


def test_wait_reports_the_exit_code_reason_for_a_killed_job(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wait` folds the exit code into its one-line reason, so a kill reads clearly inline."""
    seed_target(lote, monkeypatch)
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=137, verdict="failed")
    monkeypatch.setattr(cli, "read_log", lambda remote, root, handle: "warming up\n")
    monkeypatch.setattr(cli, "single_watcher", lambda handle: contextlib.nullcontext())
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(str(msg)))
    with pytest.raises(SystemExit) as caught:
        lote.wait("spark", "H1")
    assert caught.value.code == 1
    assert "memory" in logged[-1] and "H1 failed" in logged[-1]


def test_wait_reports_done_on_an_ok_job(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wait` on a clean job prints `done: ok` and returns without reading the log or exiting."""
    seed_target(lote, monkeypatch)
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=0, verdict="ok")
    monkeypatch.setattr(cli, "single_watcher", lambda handle: contextlib.nullcontext())
    read: list[object] = []
    monkeypatch.setattr(cli, "read_log", lambda *a: read.append(a) or "")
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(str(msg)))
    assert lote.wait("spark", "H1") is None
    assert logged[-1] == "H1 done: ok"
    assert read == []  # an ok verdict never reads the log


def test_wait_exits_two_when_the_host_stays_unreachable(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host down past the retry budget exits 2 (not a job verdict), with a clear message."""
    seed_target(lote, monkeypatch)
    monkeypatch.setattr(cli, "single_watcher", lambda handle: contextlib.nullcontext())

    def unreachable(*_: object) -> JobState:
        raise HostUnreachable("ssh channel down")

    monkeypatch.setattr(scheduler, "wait", unreachable)
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(str(msg)))
    with pytest.raises(SystemExit) as caught:
        lote.wait("spark", "H1")
    assert caught.value.code == 2
    assert "unreachable" in logged[-1]


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
    monkeypatch.setattr(lote._render, "jobs", lambda rows, *, verbose=False: rendered.append(rows))
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


# --- monitor --once (the durable single-pass sweep for a harness cron) ---


def canned_rows(lote: Lote, monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, ReconcileRow]]):
    """Pin `_job_rows` to a fixed cross-host feed so a sweep test drives classification."""
    monkeypatch.setattr(Lote, "_job_rows", lambda self, aliases, *, all=False: rows)


def test_monitor_once_json_harvests_new_terminals_and_is_idempotent(
    lote: Lote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One sweep classifies every job, pulls a finished job's results, and prints the JSON shape;
    a second sweep with nothing newly terminal reports `changed=false` and harvests nothing."""
    lote._cache.record(make_run("H1", target="spark", fetch_path="out/"))
    lote._cache.record(make_run("H2", target="spark"))
    rows = [
        ("spark", ReconcileRow(handle="H1", script="a.sh", submitted_at="t1", verdict="ok")),
        (
            "spark",
            ReconcileRow(
                handle="H2", script="b.sh", submitted_at="t2", exit_code=1, verdict="failed"
            ),
        ),
        ("spark", ReconcileRow(handle="H3", script="c.sh", submitted_at="t3", verdict="running")),
        (
            "gold",
            ReconcileRow(
                handle="H4",
                script="d.sh",
                submitted_at="t4",
                state="daemon down",
                verdict="unreachable",
            ),
        ),
    ]
    canned_rows(lote, monkeypatch, rows)
    pulled: list[tuple[str, str]] = []
    monkeypatch.setattr(Lote, "_fetch", lambda self, target, path: pulled.append((target, path)))

    lote.monitor("spark", "gold", once=True, json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] == 1
    assert payload["finished"] == [{"handle": "H1", "target": "spark", "pulled_path": "out/"}]
    assert payload["failed"] == [{"handle": "H2", "target": "spark", "reason": "exited 1"}]
    assert payload["unreachable_hosts"] == [{"host": "gold", "reason": "daemon down"}]
    assert payload["changed"] is True
    assert pulled == [("spark", "out/")]  # only the finished job's results are pulled

    lote.monitor("spark", "gold", once=True, json=True)  # nothing new is terminal this pass
    again = json.loads(capsys.readouterr().out)
    assert again["changed"] is False
    assert again["finished"] == [] and again["failed"] == []
    assert again["running"] == 1
    assert again["unreachable_hosts"] == [{"host": "gold", "reason": "daemon down"}]
    assert pulled == [("spark", "out/")]  # idempotent: no second pull


def test_monitor_once_without_json_logs_the_counts(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--once` without `--json` logs the sweep counts for a human instead of printing JSON."""
    rows = [
        ("spark", ReconcileRow(handle="H3", script="c.sh", submitted_at="t3", verdict="running"))
    ]
    canned_rows(lote, monkeypatch, rows)
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(msg))
    lote.monitor("spark", once=True)
    assert any("sweep" in msg for msg in logged)


def test_auto_pull_returns_none_without_a_fetch_path(lote: Lote) -> None:
    """A finished run with no recorded fetch path has nothing to pull, so pulled_path is None."""
    assert lote._auto_pull(make_run("H1", target="spark")) is None


def test_auto_pull_returns_the_path_on_a_successful_pull(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished run with a fetch path is pulled back and its path returned for the report."""
    monkeypatch.setattr(Lote, "_fetch", lambda self, target, path: None)
    assert lote._auto_pull(make_run("H1", target="spark", fetch_path="out/")) == "out/"


def test_auto_pull_returns_none_when_the_pull_fails(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing host-only results dir fails the rsync; the sweep swallows it and survives."""

    def boom(self: Lote, target: str, path: str) -> None:
        raise cli.ProcessExecutionError(["rsync"], 23, "", "no such file")

    monkeypatch.setattr(Lote, "_fetch", boom)
    assert lote._auto_pull(make_run("H1", target="spark", fetch_path="out/")) is None


@pytest.mark.parametrize(
    ("verdict", "exit_code", "expected"),
    [
        ("vanished", None, "vanished"),
        ("failed", 137, "memory"),  # an externally-imposed signal exit reads as its known cause
        ("failed", 1, "exited 1"),  # a plain non-zero exit
        ("failed", None, "failed"),  # no code at all
    ],
)
def test_fail_reason_is_a_short_network_free_cause(
    lote: Lote, verdict: str, exit_code: int | None, expected: str
) -> None:
    """A non-ok terminal job's reason comes from its cached verdict/exit, no extra round-trip."""
    item = ReconcileRow(
        handle="H", script="a.sh", submitted_at="t", exit_code=exit_code, verdict=verdict
    )
    assert expected in lote._fail_reason(item)


# --- revive (restart a dead scheduler daemon) ---


def test_revive_restarts_the_scheduler_daemon(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lote revive` delegates to the backend's daemon restart, the recovery for a dead pueue."""
    seed_target(lote, monkeypatch)
    lote.revive("spark")
    assert ("revive", ("/repo",)) in scheduler.calls


def test_revive_reports_the_zombie_tasks_it_cleared(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the revive retires zombie tasks the dead daemon left in flight, `lote revive` names
    them, so the operator sees the host came back with an honest job table."""
    seed_target(lote, monkeypatch)
    scheduler.revive_cleared = ["130", "132"]
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(msg.format(*a)))
    lote.revive("spark")
    assert any("zombie" in msg and "130" in msg and "132" in msg for msg in logged)


# --- serve (persistent services) ---


class RecordingServices:
    """A `Services` double: records each call's args/kwargs and replays canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.start_result = ServiceStatus(record=make_service("vllm"), healthy=True)
        self.status_result = [ServiceStatus(record=make_service("vllm"), healthy=True)]
        self.stop_result = make_service("vllm")

    def start(self, name: str, machine: Target, cmd: str, **kwargs: Any) -> ServiceStatus:
        self.calls.append(("start", (name, machine, cmd), kwargs))
        return self.start_result

    def stop(self, name: str) -> Any:
        self.calls.append(("stop", (name,), {}))
        return self.stop_result

    def status(self, name: str | None = None) -> list[ServiceStatus]:
        self.calls.append(("status", (name,), {}))
        return self.status_result

    def logs(self, name: str, *, follow: bool = False) -> None:
        self.calls.append(("logs", (name,), {"follow": follow}))


def test_serve_start_resolves_target_and_reports_healthy(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`serve_start` resolves the target, delegates to `Services.start`, logs the healthy URL."""
    seed_target(lote, monkeypatch)
    fake = RecordingServices()
    lote._services = fake
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(msg.format(*a)))
    lote.serve_start("vllm", "spark", "vllm serve model", port=8000, health_path="/health")
    [(command, args, kwargs)] = fake.calls
    assert command == "start"
    assert args == ("vllm", GB10, "vllm serve model")
    assert kwargs["port"] == 8000 and kwargs["health_path"] == "/health"
    assert any("healthy" in msg for msg in logged)


def test_serve_start_warns_when_unhealthy(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """A service that never answers health within `timeout` warns instead of failing outright."""
    seed_target(lote, monkeypatch)
    fake = RecordingServices()
    fake.start_result = ServiceStatus(record=make_service("vllm"), healthy=False)
    lote._services = fake
    warned: list[str] = []
    monkeypatch.setattr(cli.logger, "warning", lambda msg, *a: warned.append(msg.format(*a)))
    lote.serve_start("vllm", "spark", "vllm serve model", port=8000)
    assert any("did not answer" in msg for msg in warned)


def test_serve_stop_delegates_and_logs(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve_stop` delegates to `Services.stop` and logs the stopped service's target."""
    fake = RecordingServices()
    lote._services = fake
    logged: list[str] = []
    monkeypatch.setattr(cli.logger, "info", lambda msg, *a: logged.append(msg.format(*a)))
    lote.serve_stop("vllm")
    assert fake.calls == [("stop", ("vllm",), {})]
    assert any("stopped" in msg for msg in logged)


def test_serve_status_renders_the_result(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve_status` delegates to `Services.status` and hands the result to the renderer."""
    fake = RecordingServices()
    lote._services = fake
    rendered: list[list[ServiceStatus]] = []
    monkeypatch.setattr(lote._render, "services", lambda statuses: rendered.append(statuses))
    lote.serve_status("vllm")
    assert fake.calls == [("status", ("vllm",), {})]
    assert rendered == [fake.status_result]


def test_serve_status_defaults_to_every_service(
    lote: Lote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the name lists every recorded service."""
    fake = RecordingServices()
    lote._services = fake
    monkeypatch.setattr(lote._render, "services", lambda statuses: None)
    lote.serve_status()
    assert fake.calls == [("status", (None,), {})]


def test_serve_logs_delegates(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve_logs` delegates straight to `Services.logs`, `--follow` included."""
    fake = RecordingServices()
    lote._services = fake
    lote.serve_logs("vllm", follow=True)
    assert fake.calls == [("logs", ("vllm",), {"follow": True})]


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
    """fetch makes the local parent dir and rsyncs the remote path into it (no trailing slash)."""
    seed_target(lote, monkeypatch)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        dispatch, "rsync", lambda sources, dest, *a, **k: calls.append((sources, dest))
    )
    lote.fetch("spark", "out/results")
    assert Path("out").is_dir()
    [(sources, dest)] = calls
    assert sources == ["spark:/repo/out/results"]
    assert dest == "out/"


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
    monkeypatch.setattr(
        dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: syncs.append(machine)
    )
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
    monkeypatch.setattr(
        dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: syncs.append(machine)
    )
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
    """target returns the cached resolve, else onboards the host."""
    monkeypatch.setattr(Lote, "_cached", lambda self, alias: None)
    monkeypatch.setattr(Lote, "_onboard", lambda self, alias: GB10)
    assert lote.target("spark") is GB10


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
    """_onboard finds the root, rsyncs, runs setup.sh, probes, and caches the resolved Target.

    An ssh host has no scheduler queues, so onboarding is exactly the single
    login-node probe and no probe job is ever submitted.
    """
    monkeypatch.setattr(cli, "connect", lambda _name: FakeRemote())
    monkeypatch.setattr(cli, "find_root", lambda remote: "/repo")
    monkeypatch.setattr(cli, "probe_capabilities", lambda remote, alias: GB10)
    synced: list[Target] = []
    monkeypatch.setattr(
        dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: synced.append(machine)
    )
    # the bash setup runs through remote["bash"][[...]] & FG; FakeRemote needs __getitem__.
    monkeypatch.setattr(FakeRemote, "__getitem__", lambda self, _name: _Bash(), raising=False)

    machine = lote._onboard("spark")

    assert machine.name == "spark"
    assert set(machine.classes) == {"login"}  # the pueue backend reports no queues
    assert synced and synced[0].name == "spark"
    assert lote._cache.facts("spark") == GB10  # cached only after setup succeeded


def test_onboard_probes_each_scheduler_queue_and_caches_classes(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a PBS host, every queue from the scheduler becomes a probed, cached node class."""
    pbs = Target(
        name="hpc",
        kind="pbs",
        root="/repo",
        classes={"login": NodeClass(name="login", sysmem_gb=128)},
    )
    monkeypatch.setattr(cli, "find_root", lambda remote: "/repo")
    monkeypatch.setattr(cli, "probe_capabilities", lambda remote, alias: pbs)
    monkeypatch.setattr(dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: None)
    monkeypatch.setattr(FakeRemote, "__getitem__", lambda self, _name: _Bash(), raising=False)
    scheduler.queue_list = ["debug-g", "prepost"]
    monkeypatch.setattr(cli, "read_log", lambda remote, root, handle: SNAPSHOT_LOG)

    machine = lote._onboard("hpc")

    assert set(machine.classes) == {"login", "debug-g", "prepost"}
    assert machine.classes["debug-g"].gpu_name == "NVIDIA H100"
    cached = lote._cache.facts("hpc")
    assert cached is not None and set(cached.classes) == {"login", "debug-g", "prepost"}
    assert [c for c in scheduler.calls if c[0] == "submit"] == [
        ("submit", ("/repo", mock.ANY, ())),
        ("submit", ("/repo", mock.ANY, ())),
    ]


@pytest.fixture
def pbs_target() -> Target:
    """A resolved PBS target for queue-probe tests."""
    return Target(name="hpc", kind="pbs", root="/repo")


def test_probe_queue_returns_the_parsed_class(
    lote: Lote,
    scheduler: RecordingScheduler,
    pbs_target: Target,
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
) -> None:
    """A clean probe ships the generated script, submits it, and parses the snapshot."""
    shipped: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        dispatch.Dispatcher,
        "rsync_up",
        lambda self, machine, *, extra=(): shipped.append(tuple(extra)),
    )
    monkeypatch.setattr(cli, "read_log", lambda remote, root, handle: SNAPSHOT_LOG)

    node = lote._probe_queue(scheduler, pbs_target, FakeRemote(), "debug-g", wait=10.0)

    assert node is not None and node.name == "debug-g"
    assert node.gpu_name == "NVIDIA H100" and node.gpu_mem_mb == 96 * 1024
    [(generated,)] = shipped
    assert generated.startswith(".lote/jobs/")
    assert "#PBS -q debug-g" in Path(generated).read_text()
    [(_, (_root, script, _args))] = [(k, v) for k, v in scheduler.calls if k == "submit"]
    assert script == generated


def test_probe_queue_skips_rejected_submit(
    lote: Lote,
    scheduler: RecordingScheduler,
    pbs_target: Target,
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
) -> None:
    """A queue rejecting the probe job (quota, access) is skipped, never a failed discover."""

    class Rejecting(RecordingScheduler):
        def submit(self, remote, root, script, args, *, resources) -> str:  # noqa: ANN001
            raise SystemExit("qsub failed (rc=190)")

    monkeypatch.setattr(dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: None)
    assert lote._probe_queue(Rejecting(), pbs_target, FakeRemote(), "locked", wait=10.0) is None


def test_probe_queue_skips_failed_job(
    lote: Lote,
    scheduler: RecordingScheduler,
    pbs_target: Target,
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
) -> None:
    """A probe job that ends on any non-ok verdict skips the class."""
    scheduler.state_result = JobState(handle="H1", state="F", exit_code=1, verdict="failed")
    monkeypatch.setattr(dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: None)
    assert lote._probe_queue(scheduler, pbs_target, FakeRemote(), "debug-g", wait=10.0) is None


def test_probe_queue_skips_log_without_snapshot(
    lote: Lote,
    scheduler: RecordingScheduler,
    pbs_target: Target,
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
) -> None:
    """An ok job whose log carries no snapshot (mainboard missing) skips the class."""
    monkeypatch.setattr(dispatch.Dispatcher, "rsync_up", lambda self, machine, **k: None)
    monkeypatch.setattr(cli, "read_log", lambda remote, root, handle: "ModuleNotFoundError\n")
    assert lote._probe_queue(scheduler, pbs_target, FakeRemote(), "debug-g", wait=10.0) is None


def test_probe_queues_yields_only_answering_classes(
    lote: Lote, scheduler: RecordingScheduler, pbs_target: Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_probe_queues walks the scheduler's queue list and drops the unreachable ones."""
    scheduler.queue_list = ["good", "bad"]
    monkeypatch.setattr(
        Lote,
        "_probe_queue",
        lambda self, sched, machine, remote, queue, *, wait: (
            NodeClass(name=queue) if queue == "good" else None
        ),
    )
    nodes = list(lote._probe_queues(pbs_target, FakeRemote(), wait=10.0))
    assert [node.name for node in nodes] == ["good"]
    assert ("queues", ("/repo",)) in scheduler.calls


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


class _FakeSsh:
    """A minimal SshMachine stand-in: only the PATH-insert surface connect touches is wired."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.cwd = Path("/home/u")
        self.env = SimpleNamespace(path=_RecordingPath([]))


def test_connect_retries_a_transport_blip_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient transport fault at connect time is retried, not crashed on, then connects."""
    monkeypatch.setattr("lote.environment.CONNECT_BACKOFF", 0)  # no real backoff sleep in the test
    calls: list[object] = []

    def warm(argv: object, **_: object) -> SimpleNamespace:
        calls.append(argv)
        if len(calls) < 3:  # two blips (a refused control-master session), then a clean login
            return SimpleNamespace(returncode=255, stderr="kex_exchange_identification: closed\n")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("lote.environment.subprocess.run", warm)
    monkeypatch.setattr("lote.environment.SshMachine", _FakeSsh)
    assert cli.connect("spark").name == "spark"
    assert len(calls) == 3  # retried twice before the connection opened


def test_connect_gives_up_with_host_unreachable_after_persistent_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport fault that never clears surfaces as HostUnreachable once attempts are spent."""
    monkeypatch.setattr("lote.environment.CONNECT_BACKOFF", 0)
    monkeypatch.setattr(
        "lote.environment.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=255, stderr="connection timed out\n"),
    )
    with pytest.raises(HostUnreachable):
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
        "ls", "probe", "discover", "setup", "submit", "run", "status", "monitor",
        "reconcile", "interact", "logs", "why", "wait", "cancel", "kill", "revive", "info",
        "poll", "fetch", "pull", "watch", "history", "exec",
    }  # fmt: skip
    assert commands <= set(cli.app)


def test_poll_is_a_registered_command(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    """`lote poll` must be reachable from the CLI, not just callable on the object.

    Regression guard for the watcher primitive: `poll` carries a full test suite and its own
    docstring promise, yet a missing `app.command(handled(lote.poll))` left `lote poll <target>
    <handle>` an unknown command. The object-level tests never went through `build`, so they could
    not catch it. This asserts the wired app exposes `poll` alongside the other documented verbs.
    """
    app = cli.build(Lote())
    assert "poll" in set(app)
    # every verb the CLI module docstring advertises is actually wired (no doc-only ghosts).
    for advertised in ("poll", "why", "wait", "watch"):
        assert advertised in set(app), f"{advertised} is documented but not registered"
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
