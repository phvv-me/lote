"""The programmatic submit core: dispatch a job as a value, await it, fetch it back.

These exercise :mod:`lote.dispatch` directly (the CLI-free seam an experiment framework calls),
mocking the same ssh/scheduler doubles the CLI suite uses: ``pick`` -> a recording backend,
``connect`` -> a fake remote, ``git``/``rsync`` pinned. No real process, ssh, or scheduler runs.
"""

from pathlib import Path
from typing import Any

import pytest

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


def test_run_defaults_walltime_to_the_jobspec_default(
    dispatcher: Dispatcher, backend: RecordingScheduler
) -> None:
    """An unset walltime falls back to the JobSpec default rather than None."""
    dispatcher.run(GB10, "python -m foo")
    assert backend.submit_resources.walltime == JobSpec.model_fields["walltime"].default


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
    assert Path("out").is_dir()
    [(sources, dest)] = calls
    assert sources == ["spark:/repo/out//"]
    assert dest == "out//"


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

    instance = Dispatcher(
        config=SimpleNamespace(sync=SimpleNamespace(include=["src/"], exclude=[], protect=[]))
    )
    monkeypatch.setattr(dispatch, "uncovered_path_deps", lambda chefe, include: ["packages/foo"])
    with pytest.raises(LookupError, match="packages/foo"):
        instance.rsync_up(GB10)


def test_rsync_up_mirrors_the_include_set(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rsync_up ships the include set to host:root/ with archive+compress+relative+delete."""
    from types import SimpleNamespace

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
        lambda sources, dest, flags, *, exclude, protect: captured.update(
            sources=sources, dest=dest, flags=flags, protect=protect
        ),
    )
    instance.rsync_up(GB10, extra=(".lote/jobs/job-x.sh",))
    assert captured["sources"] == ["src/", ".lote/jobs/job-x.sh"]
    assert captured["dest"] == "spark:/repo/"
    assert dispatch.Rsync.DELETE in captured["flags"]
    assert captured["protect"] == ["results/***"]


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
