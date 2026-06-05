from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import lote.cli as cli
from lote.cli import Lote, git, recorded, row, run_tty
from lote.executor.cli import Executor
from lote.models import Target
from lote.schedulers import JobState

from .conftest import GB10, FakeRemote, RecordingScheduler


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


def test_exec_is_wired_eagerly() -> None:
    """The on-host executor is the one eager dependency, usable on a bare remote."""
    assert isinstance(Lote().exec, Executor)


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
    monkeypatch.setattr(cli, "probe_capabilities", lambda remote, alias: GB10.model_dump())
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
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """submit rsyncs, submits via the backend, returns the handle, and caches the run."""
    seed_target(lote, monkeypatch)
    synced: list[Target] = []
    monkeypatch.setattr(Lote, "_rsync_up", lambda self, machine: synced.append(machine))

    handle = lote.submit("spark", "train.sh", "--lr", "0.1", fetch="out/")

    assert handle == "H1"
    assert ("submit", ("/repo", "train.sh", ("--lr", "0.1"))) in scheduler.calls
    assert synced == [GB10]
    [run] = lote._cache.recent(10)
    assert run["handle"] == "H1"
    assert run["target"] == "spark"
    assert run["script"] == "train.sh"
    assert run["args"] == "--lr 0.1"
    assert run["git_sha"] == "abc1234"
    assert run["fetch_path"] == "out/"
    assert run["dirty"] == 0  # git status porcelain returned ""


def test_submit_auto_requires_needs(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit auto` without --needs is a hard error before any dispatch."""
    with pytest.raises(SystemExit, match="--needs"):
        lote.submit("auto", "train.sh")


def test_submit_auto_routes_by_needs(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`submit auto --needs N` routes to the smallest fitting known target."""
    monkeypatch.setattr(Lote, "_known_targets", lambda self: [GB10])
    monkeypatch.setattr(Lote, "_rsync_up", lambda self, machine: None)
    picked: list[float] = []
    monkeypatch.setattr(cli, "smallest_fit", lambda targets, needs: picked.append(needs) or GB10)

    handle = lote.submit("auto", "train.sh", needs=40)
    assert handle == "H1"
    assert picked == [40.0]
    assert scheduler.calls[0][0] == "submit"


# --- ps / status / reconcile ---


def test_ps_renders_recent_runs(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """ps hands the cache's recent runs to the renderer."""
    monkeypatch.setattr(lote._cache, "recent", lambda limit: [{"handle": "H1"}])
    rendered: list[object] = []
    monkeypatch.setattr(lote._render, "runs", lambda runs: rendered.append(runs))
    lote.ps(limit=5)
    assert rendered == [[{"handle": "H1"}]]


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
        {"handle": "H1", "target": "spark", "script": "a.sh", "submitted_at": "t0"},
        {"handle": "H2", "target": "other", "script": "b.sh", "submitted_at": "t1"},
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


# --- logs / info ---


def test_logs_delegates(
    lote: Lote, scheduler: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """logs delegates to the backend with the handle and follow flag."""
    seed_target(lote, monkeypatch)
    lote.logs("spark", "H1", follow=True)
    assert ("logs", ("/repo", "H1", True)) in scheduler.calls


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
        lote._cache, "run", lambda handle: {"target": "spark", "fetch_path": "out/"}
    )
    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(Lote, "_fetch", lambda self, target, path: fetched.append((target, path)))
    lote.pull("H1")
    assert fetched == [("spark", "out/")]


def test_pull_without_fetch_path_errors(lote: Lote, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with no recorded fetch path is a clear SystemExit, not a silent no-op."""
    monkeypatch.setattr(lote._cache, "run", lambda handle: {"target": "spark", "fetch_path": None})
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
    facts = GB10.model_dump()
    monkeypatch.setattr(lote._cache, "facts", lambda alias: facts)
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
    facts = GB10.model_dump()
    monkeypatch.setattr(cli, "probe_capabilities", lambda remote, alias: facts)
    synced: list[Target] = []
    monkeypatch.setattr(Lote, "_rsync_up", lambda self, machine: synced.append(machine))
    # the bash setup runs through remote["bash"][[...]] & FG; FakeRemote needs __getitem__.
    monkeypatch.setattr(FakeRemote, "__getitem__", lambda self, _name: _Bash(), raising=False)

    machine = lote._onboard("spark")

    assert machine.name == "spark"
    assert synced and synced[0].name == "spark"
    assert lote._cache.facts("spark") == facts  # cached only after setup succeeded


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


def test_connect_inserts_cargo_bin_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect opens an SshMachine and prepends ~/.cargo/bin (pueue) to its PATH."""
    inserted: list[object] = []

    class FakeSsh:
        def __init__(self, name: str) -> None:
            self.name = name
            self.cwd = Path("/home/u")
            self.env = SimpleNamespace(path=_RecordingPath(inserted))

    monkeypatch.setattr(cli, "SshMachine", FakeSsh)
    machine = cli.connect("spark")
    assert machine.name == "spark"
    assert inserted and inserted[0][0] == 0  # inserted at index 0
    assert ".cargo/bin" in str(inserted[0][1])


class _RecordingPath:
    """Records `path.insert(index, value)` calls so connect's PATH prepend is observable."""

    def __init__(self, sink: list[object]) -> None:
        self.sink = sink

    def insert(self, index: int, value: object) -> None:
        self.sink.append((index, value))


def test_app_invokes_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console entry point hands the Lote class to fire.Fire."""
    captured: list[object] = []
    monkeypatch.setattr(cli.fire, "Fire", lambda obj: captured.append(obj))
    cli.app()
    assert captured == [Lote]
