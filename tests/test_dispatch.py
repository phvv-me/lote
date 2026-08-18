"""The programmatic submit core: dispatch a job as a value, await it, fetch it back.

These exercise :mod:`lote.dispatch` directly (the CLI-free seam an experiment framework calls),
mocking the same ssh/scheduler doubles the CLI suite uses: ``pick`` -> a recording backend,
``connect`` -> a fake remote, ``git``/``rsync`` pinned. No real process, ssh, or scheduler runs.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from plumbum.commands.processes import ProcessExecutionError

import lote.dispatch as dispatch
from lote.dispatch import Dispatcher, Handle, Verdict
from lote.jobspec import JobSpec
from lote.models import Target
from lote.schedulers import HostUnreachable, JobState, Resources

from .conftest import GB10, FakeRemote, RecordingScheduler, make_run


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> RecordingScheduler:
    """Pin the dispatch seams: pick -> a recording backend, connect -> a fake remote, git fixed."""
    sched = RecordingScheduler()
    monkeypatch.setattr(dispatch, "pick", lambda _machine: sched)
    monkeypatch.setattr(dispatch, "connect", lambda _name: FakeRemote())
    monkeypatch.setattr(dispatch, "git", lambda *a: "abc1234" if a[0] == "rev-parse" else "")
    return sched


@pytest.fixture
def dispatcher(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> Dispatcher:
    """A Dispatcher whose `.lote/` writes land in the isolated workdir, never syncing for real."""
    instance = Dispatcher()
    monkeypatch.setattr(Dispatcher, "rsync_up", lambda self, machine, **k: None)
    return instance


# --- Handle / Verdict value types ---


def test_handle_alias_is_the_targets_name() -> None:
    """A Handle exposes its host alias straight off the resolved target it carries."""
    handle = Handle(id="H1", target=GB10, fetch_path="out/")
    assert handle.alias == "spark"
    assert handle.fetch_path == "out/"


def test_verdict_projects_to_ok_and_exit_code() -> None:
    """A Verdict reports `ok` and maps each verdict word to its agreed exit code."""
    assert Verdict(verdict="ok", exit_code=0).ok is True
    assert Verdict(verdict="ok").code == 0
    assert Verdict(verdict="failed", exit_code=1).ok is False
    assert Verdict(verdict="failed").code == 1
    assert Verdict(verdict="vanished").code == 3  # vanished/unknown collapse to 3


# --- run (dispatch a command, get a Handle) ---


def test_run_dispatches_and_returns_a_handle(
    dispatcher: Dispatcher, backend: RecordingScheduler, workdir: Path
) -> None:
    """run generates a job script, submits it through the backend, and returns a Handle.

    The Handle carries the resolved target and the fetch path, so a caller can poll/await/fetch
    without re-resolving the host.
    """
    handle = dispatcher.run(GB10, "python -m foo --shard 3", gpus=2, fetch="out/", name="trial-3")
    assert isinstance(handle, Handle)
    assert handle.id == "H1"
    assert handle.target is GB10
    assert handle.fetch_path == "out/"
    [(_root, script, _args)] = [v for k, v in backend.calls if k == "submit"]
    assert script.startswith(".lote/jobs/") and Path(script).is_file()


def test_run_ships_the_generated_script_as_an_extra_path(
    workdir: Path, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run ships the generated `.lote/jobs/` script as an `extra`, being outside the sync set.

    The generated script lives under `.lote/jobs/`, which the `[sync]` allowlist never covers, so
    a `run` that did not ride it along as an `extra` path would submit a script the host never
    received -- the exact drift routing the CLI through `run` must not reintroduce.
    """
    shipped: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        Dispatcher, "rsync_up", lambda self, machine, *, extra=(): shipped.append(tuple(extra))
    )
    handle = Dispatcher().run(GB10, "python -m foo")
    [(generated,)] = shipped
    assert generated.startswith(".lote/jobs/")
    [(_root, script, _args)] = [v for k, v in backend.calls if k == "submit"]
    assert script == generated  # the dispatched script is exactly the shipped one
    assert handle.id == "H1"


def test_run_threads_the_request_to_the_backend_as_resources(
    dispatcher: Dispatcher, backend: RecordingScheduler
) -> None:
    """run's gpus/walltime/queue/mem ride along as Resources so a SLURM host applies them."""
    dispatcher.run(GB10, "python -m foo", gpus=4, walltime="01:00:00", queue="gen-S", mem_gb=240)
    resources = backend.submit_resources
    assert resources.gpus == 4
    assert resources.walltime == "01:00:00"
    assert resources.queue == "gen-S"
    assert resources.mem_gb == 240


def test_run_without_walltime_leaves_a_schedulerless_host_uncapped(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No walltime on an ssh host means no cap at all, stated out loud at submit.

    The regression: the silent 30-minute JobSpec default used to ride into the bash wrapper's
    `timeout` and SIGTERM healthy long runs. Now the wrapper gets no `timeout` and the
    dispatcher logs that the run is uncapped.
    """
    logged: list[str] = []
    monkeypatch.setattr(dispatch.logger, "info", lambda msg, *a: logged.append(str(msg) % ()))
    dispatcher.run(GB10, "python -m foo")
    assert backend.submit_resources.walltime is None
    [(_root, script, _args)] = [v for k, v in backend.calls if k == "submit"]
    assert "timeout" not in Path(script).read_text()
    assert any("no walltime cap" in msg for msg in logged)


def test_run_logs_the_effective_walltime_for_a_pbs_default(
    backend: RecordingScheduler, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PBS submit without an explicit walltime logs the defaulted header value, never silent."""
    monkeypatch.setattr(Dispatcher, "rsync_up", lambda self, machine, **k: None)
    logged: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(dispatch.logger, "info", lambda msg, *a: logged.append((str(msg), a)))
    pbs = Target(name="hpc", kind="pbs", root="/work")
    Dispatcher().run(pbs, "python -m foo")
    walltime_logs = [(msg, a) for msg, a in logged if msg.startswith("walltime")]
    assert walltime_logs == [("walltime {} ({})", ("00:30:00", "PBS default header"))]


def test_run_logs_an_explicit_walltime_as_enforced_on_a_schedulerless_host(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit cap on an ssh host is logged as timeout-enforced and renders the wrapper."""
    logged: list[str] = []
    monkeypatch.setattr(dispatch.logger, "info", lambda msg, *a: logged.append(str(msg)))
    dispatcher.run(GB10, "python -m foo", walltime="02:00:00")
    assert any("enforced by timeout" in msg for msg in logged)
    [(_root, script, _args)] = [v for k, v in backend.calls if k == "submit"]
    text = Path(script).read_text()
    assert "timeout --kill-after=30s 7200" in text
    assert "lote: killed at walltime 02:00:00" in text


def test_run_records_the_run_with_provenance(
    dispatcher: Dispatcher, backend: RecordingScheduler, workdir: Path
) -> None:
    """A dispatched run is cached with its git sha and fetch path, so status/pull find it later."""
    handle = dispatcher.run(GB10, "python -m foo", fetch="out/")
    [run] = dispatcher.cache.recent(10)
    assert run.handle == handle.id
    assert run.target == "spark"
    assert run.git_sha == "abc1234"
    assert run.fetch_path == "out/"
    assert run.dirty == 0  # git status porcelain returned ""


# --- auto routing ---


def test_run_auto_routes_to_the_smallest_fitting_target(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run("auto", ...)` sizes by needs_gb to the smallest fitting known target."""
    picked: list[float] = []
    monkeypatch.setattr(
        dispatch, "smallest_fit", lambda targets, needs: picked.append(needs) or GB10
    )
    handle = dispatcher.run("auto", "python -m foo", needs_gb=40, known_targets=[GB10])
    assert handle.target is GB10
    assert picked == [40.0]


def test_run_auto_without_needs_is_a_lookup_error(dispatcher: Dispatcher) -> None:
    """`run("auto", ...)` with no needs_gb fails before any dispatch."""
    with pytest.raises(LookupError, match="needs_gb"):
        dispatcher.run("auto", "python -m foo")


def test_run_rejects_an_unknown_string_target(dispatcher: Dispatcher) -> None:
    """A string target other than `auto` is a clear error, not a silent host miss."""
    with pytest.raises(LookupError, match="resolved Target or 'auto'"):
        dispatcher.run("spark", "python -m foo")


# --- submit (the shared chokepoint) ---


def test_submit_dirty_tree_is_recorded(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncommitted working tree at dispatch time is flagged on the run record."""
    monkeypatch.setattr(dispatch, "git", lambda *a: "abc1234" if a[0] == "rev-parse" else "M x.py")
    dispatcher.submit(GB10, "train.sh", (), resources=Resources())
    [run] = dispatcher.cache.recent(10)
    assert run.dirty == 1


def test_resubmit_failed_external_script_restages_and_ships_before_each_dispatch(
    dispatcher: Dispatcher,
    backend: RecordingScheduler,
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed external script is recreated and shipped again before resubmission.

    This mirrors the gold 193 to 194 sequence. Both scheduler calls must receive the same
    repository-relative `.lote/jobs` path, and that path must already exist on the modeled host.
    Removing both copies after the first failure proves the second submit does not trust stale
    content-addressed state or degrade to the raw local path.
    """
    external = workdir.parent / f"{workdir.name}-external.sh"
    external.write_text("#!/bin/bash\necho converted\n")
    host = workdir / "host"
    shipped: list[tuple[str, ...]] = []

    def ship(self: Dispatcher, machine: Target, *, extra: tuple[str, ...] = ()) -> None:
        del self, machine
        shipped.append(extra)
        for source in extra:
            destination = host / source
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(Path(source).read_bytes())

    dispatched: list[str] = []
    original_submit = backend.submit

    def submit(remote, root, script, args, *, resources) -> str:  # noqa: ANN001
        dispatched.append(script)
        assert not Path(script).is_absolute()
        assert (host / script).is_file()
        return original_submit(remote, root, script, args, resources=resources)

    monkeypatch.setattr(Dispatcher, "rsync_up", ship)
    monkeypatch.setattr(backend, "submit", submit)

    first = dispatcher.submit(GB10, str(external), (), resources=Resources())
    dispatcher.cache.resolve(dispatcher.cache.run(first), "Done", 1, "failed")
    staged = Path(dispatched[0])
    staged.unlink()
    (host / staged).unlink()

    backend.submit_handle = "H2"
    second = dispatcher.submit(GB10, str(external), (), resources=Resources())

    assert (first, second) == ("H1", "H2")
    assert dispatched == [str(staged), str(staged)]
    assert shipped == [(str(staged),), (str(staged),)]
    assert staged.is_file() and (host / staged).is_file()
    assert staged.read_text() == external.read_text()
    assert [dispatcher.cache.run(handle).script for handle in (first, second)] == dispatched


def test_submit_rejects_a_missing_explicit_path_before_scheduler_dispatch(
    dispatcher: Dispatcher, backend: RecordingScheduler
) -> None:
    """An unresolved explicit path fails locally instead of leaking into the host command."""
    with pytest.raises(FileNotFoundError, match="cannot ship it to the host"):
        dispatcher.submit(GB10, "./missing/job.sh", (), resources=Resources())
    assert [call for call in backend.calls if call[0] == "submit"] == []


def test_submit_error_names_the_target(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduler rejection identifies the target that produced it."""

    def reject(
        remote: FakeRemote,
        root: str,
        script: str,
        args: Sequence[str],
        *,
        resources: Resources,
    ) -> str:
        del remote, root, script, args, resources
        raise SystemExit("no PBS queue resolved after trying --queue and #PBS -q")

    monkeypatch.setattr(backend, "submit", reject)
    with pytest.raises(SystemExit, match=r"target 'spark'.*--queue.*#PBS -q"):
        dispatcher.submit(GB10, "train.sh", (), resources=Resources())


def test_submit_aborts_when_the_required_staged_script_transfer_is_partial(
    backend: RecordingScheduler, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rsync code 24 is fatal for a required script and prevents scheduler dispatch."""
    from types import SimpleNamespace

    (workdir / "src").mkdir()
    external = workdir.parent / f"{workdir.name}-partial.sh"
    external.write_text("#!/bin/bash\nexit 1\n")
    instance = Dispatcher(
        config=SimpleNamespace(sync=SimpleNamespace(include=["src/"], exclude=[], protect=[]))
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])

    def fail(*args, **kwargs) -> None:
        raise ProcessExecutionError(["rsync"], 24, "", "source vanished")

    monkeypatch.setattr(dispatch, "rsync", fail)
    with pytest.raises(RuntimeError, match="submission aborted before scheduler dispatch"):
        instance.submit(GB10, str(external), (), resources=Resources())
    assert [call for call in backend.calls if call[0] == "submit"] == []


# --- chefe preflight (a broken target fails with one sentence, not a bare traceback) ---


def test_verify_chefe_passes_silently_on_a_healthy_target(dispatcher: Dispatcher) -> None:
    """A target whose `chefe --help` exits clean is not flagged; `_verify_chefe` just returns."""
    assert dispatcher._verify_chefe(FakeRemote(), GB10) is None


def test_verify_chefe_names_the_target_and_the_fix(dispatcher: Dispatcher) -> None:
    """A broken target's chefe fails with the target name, the extracted cause, and the repair."""
    remote = FakeRemote(ok=False, stderr="ModuleNotFoundError: No module named 'chefe.core'")
    with pytest.raises(SystemExit) as excinfo:
        dispatcher._verify_chefe(remote, GB10)
    message = str(excinfo.value)
    assert "chefe on 'spark' is broken" in message
    assert "ModuleNotFoundError: No module named 'chefe.core'" in message
    assert "lote setup spark" in message


def test_submit_checks_chefe_before_the_scheduler_ever_sees_the_job(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken target's chefe aborts `submit` before the scheduler is touched.

    The regression this guards: a stale editable install (or, as happened on crimson, a source
    path an over-eager `.gitignore` entry silently dropped from lote's own sync) used to
    surface only as a raw traceback buried inside the *job's* captured log, hours after
    dispatch. Now the same activated `chefe --help` every job depends on is checked with the
    already-open connection, right after the repo syncs and before the scheduler ever sees
    the job, so the failure is immediate and names the fix.
    """
    monkeypatch.setattr(
        dispatch,
        "connect",
        lambda _name: FakeRemote(ok=False, stderr="ModuleNotFoundError: No module named 'x'"),
    )
    with pytest.raises(SystemExit, match=r"chefe on 'spark' is broken.*lote setup spark"):
        dispatcher.submit(GB10, "train.sh", (), resources=Resources())
    assert [call for call in backend.calls if call[0] == "submit"] == []


# --- await_many ---


def test_await_many_blocks_until_each_handle_is_terminal(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """await_many polls each handle and returns its terminal Verdict, keyed by handle."""
    monkeypatch.setattr(dispatch, "sleep", lambda _s: None)
    backend.state_result = JobState(handle="H1", state="F", exit_code=0, verdict="ok")
    handle = Handle(id="H1", target=GB10)
    verdicts = dispatcher.await_many([handle])
    assert verdicts[handle].ok
    assert verdicts[handle].exit_code == 0


def test_await_many_polls_running_handles_until_they_finish(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-running handle is re-polled each tick until it reaches a terminal verdict."""
    monkeypatch.setattr(dispatch, "sleep", lambda _s: None)
    running = JobState(handle="H1", state="R", verdict="running")
    done = JobState(handle="H1", state="F", exit_code=0, verdict="ok")
    states = iter([running, running, done])

    def state(remote: object, root: str, handle: str) -> JobState:
        return next(states)

    monkeypatch.setattr(backend, "state", state)
    handle = Handle(id="H1", target=GB10)
    verdicts = dispatcher.await_many([handle])
    assert verdicts[handle].verdict == "ok"


def test_await_many_retries_a_transient_blip_without_a_false_verdict(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A HostUnreachable on one tick is retried next tick, never read as a vanished job."""
    monkeypatch.setattr(dispatch, "sleep", lambda _s: None)
    calls = {"n": 0}

    def state(remote: object, root: str, handle: str) -> JobState:
        calls["n"] += 1
        if calls["n"] == 1:
            raise HostUnreachable("ssh channel down")
        return JobState(handle="H1", state="F", exit_code=0, verdict="ok")

    monkeypatch.setattr(backend, "state", state)
    handle = Handle(id="H1", target=GB10)
    verdicts = dispatcher.await_many([handle])
    assert verdicts[handle].ok
    assert calls["n"] == 2  # the blip tick retried


def test_await_many_carries_the_failure_reason_for_a_killed_job(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ok verdict reads the log for a one-line reason (the same triage `lote why` prints)."""
    monkeypatch.setattr(dispatch, "sleep", lambda _s: None)
    backend.state_result = JobState(handle="H1", state="F", exit_code=137, verdict="failed")
    monkeypatch.setattr(dispatch, "read_log", lambda remote, root, handle: "warming up\n")
    handle = Handle(id="H1", target=GB10)
    verdict = dispatcher.await_many([handle])[handle]
    assert verdict.verdict == "failed"
    assert "memory" in verdict.reason and "walltime" in verdict.reason


def test_await_many_persists_a_terminal_verdict_to_the_cache(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved verdict is written back so later scheduler GC cannot flip `ok` to `vanished`."""
    monkeypatch.setattr(dispatch, "sleep", lambda _s: None)
    dispatcher.cache.record(make_run("H1", target="spark"))
    backend.state_result = JobState(handle="H1", state="F", exit_code=0, verdict="ok")
    dispatcher.await_many([Handle(id="H1", target=GB10)])
    assert dispatcher.cache.run("H1").verdict == "ok"


def test_await_many_tolerates_an_unrecorded_handle(
    dispatcher: Dispatcher, backend: RecordingScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Awaiting a handle the cache never recorded still yields its verdict (no cache write)."""
    monkeypatch.setattr(dispatch, "sleep", lambda _s: None)
    backend.state_result = JobState(handle="H1", state="F", exit_code=0, verdict="ok")
    handle = Handle(id="H1", target=GB10)
    assert dispatcher.await_many([handle])[handle].ok


# --- fetch ---


def test_fetch_pulls_the_recorded_path_back(
    dispatcher: Dispatcher, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch rsyncs the handle's recorded results path back into the same local path."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        dispatch, "rsync", lambda sources, dest, *a, **k: calls.append((sources, dest))
    )
    dispatcher.fetch(Handle(id="H1", target=GB10, fetch_path="out/"))
    [(sources, dest)] = calls
    assert sources == ["spark:/repo/out"]
    assert dest == "./"


def test_fetch_of_a_single_file_lands_in_its_parent_not_a_local_directory(
    dispatcher: Dispatcher, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-file fetch path pulls the file into its parent, never a directory of that name."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        dispatch, "rsync", lambda sources, dest, *a, **k: calls.append((sources, dest))
    )
    dispatcher.fetch(Handle(id="H1", target=GB10, fetch_path="a/b/c.json"))
    assert Path("a/b").is_dir()
    assert not Path("a/b/c.json").exists()
    [(sources, dest)] = calls
    assert sources == ["spark:/repo/a/b/c.json"]
    assert dest == "a/b/"


def test_fetch_without_a_path_is_a_lookup_error(dispatcher: Dispatcher) -> None:
    """A handle dispatched without a fetch path is a clear error, not a silent no-op."""
    with pytest.raises(LookupError, match="no fetch path"):
        dispatcher.fetch(Handle(id="H1", target=GB10))


# --- rsync_up (the real sync guards) ---


def test_rsync_up_fails_fast_on_empty_include(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No [sync] include paths is a hard LookupError, not an rsync no-op that ships nothing."""
    from types import SimpleNamespace

    instance = Dispatcher(config=SimpleNamespace(sync=SimpleNamespace(include=[], exclude=[])))
    ran: list[object] = []
    monkeypatch.setattr(dispatch, "rsync", lambda *a, **k: ran.append(a))
    with pytest.raises(LookupError, match=r"\[sync\]"):
        instance.rsync_up(GB10)
    assert ran == []


def test_rsync_up_fails_fast_on_an_unshipped_path_dep(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chefe editable path dep not under [sync].include is a clear LookupError before sync."""
    from types import SimpleNamespace

    (workdir / "src").mkdir()
    instance = Dispatcher(
        config=SimpleNamespace(sync=SimpleNamespace(include=["src/"], exclude=[], protect=[]))
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: ["packages/foo"])
    with pytest.raises(LookupError, match="packages/foo"):
        instance.rsync_up(GB10)


def test_rsync_up_adds_the_include_set(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rsync_up mirrors declared paths while protecting remote-only work products."""
    from types import SimpleNamespace

    (workdir / "src").mkdir()
    instance = Dispatcher(
        config=SimpleNamespace(
            sync=SimpleNamespace(include=["src/"], exclude=["data/"], protect=["results/***"])
        )
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dispatch,
        "rsync",
        lambda sources, dest, flags, *, include, filters, exclude, protect, allow_vanished: (
            captured.update(
                sources=sources,
                dest=dest,
                flags=flags,
                include=include,
                filters=filters,
                protect=protect,
                allow_vanished=allow_vanished,
            )
        ),
    )
    instance.rsync_up(GB10, extra=(".lote/jobs/job-x.sh",))
    assert captured["sources"] == ["src/", ".lote/jobs/job-x.sh"]
    assert captured["dest"] == "spark:/repo/"
    assert dispatch.Rsync.DELETE in captured["flags"]
    assert dispatch.Rsync.DELETE_AFTER in captured["flags"]
    assert dispatch.Rsync.VERBOSE in captured["flags"]
    assert captured["include"] == ()
    assert captured["filters"] == [":- .gitignore"]
    assert captured["protect"] == ["results/***"]
    assert captured["allow_vanished"] is False
    assert len(list((workdir / ".lote" / "locks").glob("sync-*.lock"))) == 1


class _HashRemote:
    """A connection double for the env-swap guard: one `sha256sum` answer, context-managed."""

    def __init__(self, stdout: str, retcode: int = 0) -> None:
        self.stdout = stdout
        self.retcode = retcode

    def __enter__(self) -> _HashRemote:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def __getitem__(self, _name: str) -> _HashRemote:
        return self

    def run(self, retcode: int | None = None) -> tuple[int, str, str]:
        del retcode
        return self.retcode, self.stdout, ""


def _guarded_dispatcher(workdir: Path) -> Dispatcher:
    """A Dispatcher over a workdir carrying the compiled chefe pair the guard triggers on."""
    from types import SimpleNamespace

    (workdir / "src").mkdir()
    chefe = workdir / ".chefe"
    chefe.mkdir()
    (chefe / "pixi.toml").write_text("t")
    (chefe / "pixi.lock").write_text("local-lock")
    return Dispatcher(
        config=SimpleNamespace(
            sync=SimpleNamespace(include=["src/"], exclude=[], protect=[]),
            ssh=SimpleNamespace(rsync_shell="ssh", deadline=10.0),
        )
    )


def test_rsync_up_warns_when_the_lock_changes_under_running_jobs(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shipping a changed pixi.lock to a host with running jobs names those jobs, since the
    next `chefe run` there rebuilds the env underneath them."""
    from types import SimpleNamespace

    instance = _guarded_dispatcher(workdir)
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])
    monkeypatch.setattr(dispatch, "rsync", lambda *args, **kwargs: "")
    monkeypatch.setattr(instance, "_connection", lambda name: _HashRemote("beef1234  lock\n"))
    jobs = [SimpleNamespace(handle="221", verdict="running")]
    monkeypatch.setattr(
        dispatch, "pick", lambda machine: SimpleNamespace(jobs=lambda remote, root: jobs)
    )
    warned: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(dispatch.logger, "warning", lambda msg, *a: warned.append((str(msg), a)))
    instance.rsync_up(GB10)
    assert warned and "221" in warned[0][1]


def test_rsync_up_stays_quiet_when_the_remote_lock_matches(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged lock never queries the scheduler and never warns, so queueing more work
    on a busy host stays silent."""
    import hashlib

    instance = _guarded_dispatcher(workdir)
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])
    monkeypatch.setattr(dispatch, "rsync", lambda *args, **kwargs: "")
    digest = hashlib.sha256(b"local-lock").hexdigest()
    monkeypatch.setattr(instance, "_connection", lambda name: _HashRemote(f"{digest}  lock\n"))
    monkeypatch.setattr(
        dispatch, "pick", lambda machine: pytest.fail("an unchanged lock must not query jobs")
    )
    warned: list[str] = []
    monkeypatch.setattr(dispatch.logger, "warning", lambda msg, *a: warned.append(str(msg)))
    instance.rsync_up(GB10)
    assert warned == []


def test_rsync_up_ships_parent_gitignores_for_narrow_include_roots(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receiver control files cover root and submodule rules above an included subtree."""
    from types import SimpleNamespace

    (workdir / ".gitignore").write_text("*.scratch\n")
    (workdir / "research").mkdir()
    (workdir / "research/.gitignore").write_text("generated/\n")
    (workdir / "research/projects").mkdir()
    instance = Dispatcher(
        config=SimpleNamespace(
            sync=SimpleNamespace(include=["research/projects"], exclude=[], protect=[])
        )
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dispatch,
        "rsync",
        lambda sources, dest, flags, **options: captured.update(
            sources=sources, dest=dest, flags=flags, **options
        ),
    )

    instance.rsync_up(GB10)

    assert captured["sources"] == [
        "research/projects",
        ".gitignore",
        "research/.gitignore",
    ]
    assert captured["filters"] == ["merge,- .gitignore", ":- .gitignore"]
    assert captured["allow_vanished"] is False


def test_rsync_up_ships_the_compiled_chefe_pair_despite_gitignore(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chefe's compiled manifest and lock are required sources while other state stays ignored."""
    from types import SimpleNamespace

    (workdir / "src").mkdir()
    (workdir / "chefe.toml").write_text('[workspace]\nname = "demo"\n')
    compiled = workdir / ".chefe"
    compiled.mkdir()
    (compiled / "pixi.toml").write_text('[workspace]\nname = "demo"\n')
    (compiled / "pixi.lock").write_text("version: 7\n")
    instance = Dispatcher(
        config=SimpleNamespace(sync=SimpleNamespace(include=["src/"], exclude=[], protect=[]))
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dispatch,
        "rsync",
        lambda sources, dest, flags, **options: captured.update(sources=sources, **options),
    )

    instance.rsync_up(GB10)

    assert captured["sources"] == [
        "src/",
        ".chefe/pixi.toml",
        ".chefe/pixi.lock",
    ]
    assert captured["include"] == (
        "/.chefe/",
        "/.chefe/pixi.toml",
        "/.chefe/pixi.lock",
    )
    assert captured["exclude"][0] == "/.chefe/***"
    assert captured["allow_vanished"] is False


def test_rsync_up_refuses_an_incomplete_compiled_chefe_pair(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chefe workspace must solve locally before lote can mirror it to a remote host."""
    from types import SimpleNamespace

    (workdir / "src").mkdir()
    (workdir / "chefe.toml").write_text('[workspace]\nname = "demo"\n')
    instance = Dispatcher(
        config=SimpleNamespace(sync=SimpleNamespace(include=["src/"], exclude=[], protect=[]))
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])
    ran: list[object] = []
    monkeypatch.setattr(dispatch, "rsync", lambda *args, **kwargs: ran.append(args))

    with pytest.raises(LookupError, match=r"compiled .*pixi.toml.*pixi.lock.*chefe install"):
        instance.rsync_up(GB10)

    assert ran == []


def test_rsync_up_preserves_an_ordinary_mirror_error(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal mirror without a required script keeps its original rsync failure."""
    from types import SimpleNamespace

    (workdir / "src").mkdir()
    instance = Dispatcher(
        config=SimpleNamespace(sync=SimpleNamespace(include=["src/"], exclude=[], protect=[]))
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])
    failure = ProcessExecutionError(["rsync"], 23, "", "partial transfer")

    def fail(*args, **kwargs) -> None:
        raise failure

    monkeypatch.setattr(dispatch, "rsync", fail)
    with pytest.raises(ProcessExecutionError) as caught:
        instance.rsync_up(GB10)
    assert caught.value is failure


def test_rsync_up_drops_stale_include_paths_with_one_clear_warning(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A [sync].include path deleted locally is skipped and named in one warning line.

    The regression: a stale `packages/meteng` include surfaced only as an rsync code-23 line
    buried in discover output. Now the sync states exactly which declared paths are missing
    and ships the rest.
    """
    from types import SimpleNamespace

    (workdir / "src").mkdir()
    instance = Dispatcher(
        config=SimpleNamespace(
            sync=SimpleNamespace(include=["src/", "packages/meteng"], exclude=[], protect=[])
        )
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: [])
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        dispatch,
        "rsync",
        lambda sources, dest, flags, **k: captured.update(sources=sources),
    )
    warned: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(dispatch.logger, "warning", lambda msg, *a: warned.append((str(msg), a)))
    instance.rsync_up(GB10)
    assert captured["sources"] == ["src/"]
    [(message, args)] = warned
    assert "stale [sync].include" in message
    assert args[0] == 1 and args[1] == "packages/meteng"


def test_rsync_up_with_every_include_missing_is_a_lookup_error(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When nothing declared exists locally there is nothing to mirror; fail before rsync."""
    from types import SimpleNamespace

    instance = Dispatcher(
        config=SimpleNamespace(sync=SimpleNamespace(include=["gone/"], exclude=[], protect=[]))
    )
    ran: list[object] = []
    monkeypatch.setattr(dispatch, "rsync", lambda *a, **k: ran.append(a))
    with pytest.raises(LookupError, match="missing locally"):
        instance.rsync_up(GB10)
    assert ran == []


# --- write_job_script ---


def test_write_job_script_picks_the_renderer_and_is_content_addressed(
    dispatcher: Dispatcher, workdir: Path
) -> None:
    """A PBS host gets a #PBS script, an ssh host a bash wrapper, and same text reuses a file."""
    pbs = Target(name="hpc", kind="pbs", root="/work")
    pbs_path = dispatcher.write_job_script(pbs, JobSpec(cmd="python -m foo"))
    assert pbs_path.startswith(".lote/jobs/")
    assert "#PBS -q debug-g" in Path(pbs_path).read_text()

    bash_path = dispatcher.write_job_script(GB10, JobSpec(cmd="python -m foo"))
    assert "#PBS" not in Path(bash_path).read_text()

    again = dispatcher.write_job_script(GB10, JobSpec(cmd="python -m foo"))
    assert again == bash_path  # content-addressed: same text, same file


# --- connect / git module seams ---


def test_connect_opens_an_activated_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect builds the activated ssh session through Environment, the shared PATH source."""
    opened: list[str] = []
    monkeypatch.setattr(
        dispatch.Environment, "connection", lambda self, host: opened.append(host) or "SESSION"
    )
    assert dispatch.connect("spark") == "SESSION"
    assert opened == ["spark"]


def test_git_strips_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """git returns the stripped stdout of the local git call."""
    monkeypatch.setattr(
        dispatch.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": " abc \n"})
    )
    assert dispatch.git("rev-parse", "HEAD") == "abc"
