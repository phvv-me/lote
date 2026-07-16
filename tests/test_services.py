"""``lote serve``'s CLI-free core: two supervised pueue tasks, a health poll, a cache record.

Mocks the same seams the rest of the suite mocks -- ``connect`` -> a recording remote,
``pueue.add``/``kill``/``log``/``start`` -> observed calls -- plus ``urlopen`` for the health
probe, so no real ssh, process, or HTTP request ever runs.
"""

import contextlib
import socket
from pathlib import Path
from urllib.error import URLError

import pytest
from plumbum.commands.processes import ProcessExecutionError

import lote.services as services
from lote.cache import Cache
from lote.models import Target
from lote.services import (
    Services,
    claim_local_port,
    ensure_local_daemon,
    probe_health,
    tunnel_loop,
)

from .conftest import RecordingMachine, make_service

GOLD = Target(name="gold", kind="ssh", root="/home/user/projects")


class FakeResponse:
    """A stand-in for ``http.client.HTTPResponse``: a context manager with just ``.status``."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


@pytest.fixture
def remote_machine() -> RecordingMachine:
    """The `RecordingMachine` `pueue.add`/`kill`/`log` calls land on for the remote side."""
    return RecordingMachine()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, remote_machine: RecordingMachine) -> RecordingMachine:
    """Pin `connect` to hand back `remote_machine` inside a no-op context manager."""
    monkeypatch.setattr(services, "connect", lambda _name: contextlib.nullcontext(remote_machine))
    return remote_machine


@pytest.fixture
def manager(workdir: Path) -> Services:
    """A `Services` whose cache lands in the isolated `workdir`."""
    return Services(cache=Cache(workdir / "db.sqlite"))


# --- probe_health ---


def test_probe_health_true_for_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2xx/3xx response is healthy."""
    monkeypatch.setattr(services, "urlopen", lambda url, timeout: FakeResponse(200))
    assert probe_health("http://localhost:8000/health") is True


def test_probe_health_false_for_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx/5xx response is not healthy (the server answered, just not happily)."""
    monkeypatch.setattr(services, "urlopen", lambda url, timeout: FakeResponse(500))
    assert probe_health("http://localhost:8000/health") is False


def test_probe_health_false_on_connection_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused/timed-out connection reads as not-yet-healthy, never an exception."""

    def refuse(url: str, timeout: float) -> FakeResponse:
        raise URLError("connection refused")

    monkeypatch.setattr(services, "urlopen", refuse)
    assert probe_health("http://localhost:8000/health") is False


def test_probe_health_false_on_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw OSError (a reset socket) is absorbed the same as a URLError."""

    def reset(url: str, timeout: float) -> FakeResponse:
        raise OSError("connection reset")

    monkeypatch.setattr(services, "urlopen", reset)
    assert probe_health("http://localhost:8000/health") is False


# --- tunnel_loop ---


def test_tunnel_loop_wraps_a_keepalive_ssh_in_a_retry_loop() -> None:
    """The tunnel command reconnects forever, forwarding the given ports through `target`."""
    command = tunnel_loop("gold", 8000, 8001)
    assert command.startswith("while true; do ssh -N -L 8000:localhost:8001")
    assert "ExitOnForwardFailure=yes" in command
    assert command.endswith("gold; sleep 2; done")


# --- claim_local_port ---


def test_claim_local_port_passes_on_a_free_port() -> None:
    """A free port claims silently (bound and released, nothing leaks)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    claim_local_port(free_port)


def test_claim_local_port_raises_on_a_resident_listener() -> None:
    """A port a local service already owns raises a LookupError naming --local-port.

    This is the false-healthy trap: with a resident listener, ssh binds only the other
    address family and the health probe answers from the wrong service.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        taken_port = holder.getsockname()[1]
        with pytest.raises(LookupError, match="--local-port"):
            claim_local_port(taken_port)


# --- ensure_local_daemon ---


def test_ensure_local_daemon_starts_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean start calls `pueue.start()` once."""
    calls = []
    monkeypatch.setattr(services.pueue, "start", lambda **kwargs: calls.append(kwargs))
    ensure_local_daemon()
    assert calls == [{}]


def test_ensure_local_daemon_absorbs_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that already owns the socket errors; that failure is swallowed, not raised."""

    def already_running(**kwargs: object) -> str:
        raise ProcessExecutionError(["pueued", "-d"], 1, "", "already running")

    monkeypatch.setattr(services.pueue, "start", already_running)
    ensure_local_daemon()  # does not raise


# --- Services.start ---


def test_start_submits_remote_then_tunnel_and_persists_a_healthy_record(
    monkeypatch: pytest.MonkeyPatch, wired: RecordingMachine, manager: Services
) -> None:
    """`start` submits the remote task first, then the local tunnel task, records both ids."""
    calls: list[dict[str, object]] = []

    def fake_add(command: str, **kwargs: object) -> str:
        calls.append({"command": command, **kwargs})
        return str(len(calls))

    monkeypatch.setattr(services.pueue, "add", fake_add)
    monkeypatch.setattr(services, "ensure_local_daemon", lambda: None)
    monkeypatch.setattr(services, "probe_health", lambda url: True)
    monkeypatch.setattr(services, "claim_local_port", lambda port: None)

    outcome = manager.start(
        "vllm", GOLD, "vllm serve model --port 8000", port=8000, health_path="/health"
    )

    assert outcome.healthy is True
    assert outcome.record.remote_task == "1"
    assert outcome.record.tunnel_task == "2"
    assert calls[0]["machine"] is wired
    assert calls[0]["label"] == "lote-serve-vllm"
    assert "vllm serve model --port 8000" in str(calls[0]["command"])
    assert "machine" not in calls[1]  # the tunnel task runs on the local default
    assert calls[1]["label"] == "lote-tunnel-vllm"
    persisted = manager.cache.service("vllm")
    assert (persisted.remote_task, persisted.tunnel_task) == ("1", "2")


def test_start_defaults_local_port_to_port(
    monkeypatch: pytest.MonkeyPatch, wired: RecordingMachine, manager: Services
) -> None:
    """Omitting `local_port` tunnels to the same port the service binds remotely."""
    monkeypatch.setattr(services.pueue, "add", lambda command, **kwargs: "1")
    monkeypatch.setattr(services, "ensure_local_daemon", lambda: None)
    monkeypatch.setattr(services, "probe_health", lambda url: True)
    claimed: list[int] = []
    monkeypatch.setattr(services, "claim_local_port", claimed.append)
    outcome = manager.start("vllm", GOLD, "vllm serve model", port=9000)
    assert outcome.record.local_port == 9000
    assert claimed == [9000]  # the pre-flight claim guards the resolved local port


def test_start_reports_unhealthy_without_failing_when_timeout_elapses(
    monkeypatch: pytest.MonkeyPatch, wired: RecordingMachine, manager: Services
) -> None:
    """A service that never answers within `timeout` is reported unhealthy, task left running."""
    monkeypatch.setattr(services.pueue, "add", lambda command, **kwargs: "1")
    monkeypatch.setattr(services, "ensure_local_daemon", lambda: None)
    monkeypatch.setattr(services, "probe_health", lambda url: False)
    monkeypatch.setattr(services, "claim_local_port", lambda port: None)
    outcome = manager.start("vllm", GOLD, "vllm serve model", port=8000, timeout=0.0)
    assert outcome.healthy is False
    assert manager.cache.service("vllm").remote_task == "1"  # the record still lands


def test_start_refuses_a_taken_local_port_before_launching_anything(
    monkeypatch: pytest.MonkeyPatch, wired: RecordingMachine, manager: Services
) -> None:
    """A resident local listener aborts the start before any remote or tunnel task exists."""
    added: list[str] = []
    monkeypatch.setattr(services.pueue, "add", lambda command, **kwargs: added.append(command))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        taken_port = holder.getsockname()[1]
        with pytest.raises(LookupError, match="--local-port"):
            manager.start("vllm", GOLD, "vllm serve model", port=taken_port)
    assert added == []  # nothing was launched on either end


# --- Services._await_healthy ---


def test_await_healthy_polls_until_true(
    monkeypatch: pytest.MonkeyPatch, manager: Services
) -> None:
    """The poll loop retries on a false probe and returns as soon as one succeeds."""
    results = iter([False, False, True])
    monkeypatch.setattr(services, "probe_health", lambda url: next(results))
    monkeypatch.setattr(services, "sleep", lambda seconds: None)
    record = make_service("vllm", local_port=9000, health_path="/health")
    assert manager._await_healthy(record, timeout=100.0) is True


# --- Services.stop ---


def test_stop_kills_remote_task_then_tunnel_and_drops_the_record(
    monkeypatch: pytest.MonkeyPatch, wired: RecordingMachine, manager: Services
) -> None:
    """`stop` kills the remote task with its root, kills the tunnel task locally, drops the row."""
    manager.cache.save_service(
        make_service("vllm", target="gold", root="/root", remote_task="3", tunnel_task="1")
    )
    kill_calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        services.pueue, "kill", lambda task_id, **kwargs: kill_calls.append((task_id, kwargs))
    )
    manager.stop("vllm")
    assert kill_calls[0] == ("3", {"machine": wired, "root": "/root"})
    assert kill_calls[1] == ("1", {})
    with pytest.raises(LookupError):
        manager.cache.service("vllm")


def test_stop_absorbs_process_errors_from_either_kill(
    monkeypatch: pytest.MonkeyPatch, wired: RecordingMachine, manager: Services
) -> None:
    """A task pueue no longer knows about (already dead) does not block the record cleanup."""
    manager.cache.save_service(make_service("vllm"))

    def raise_it(task_id: object, **kwargs: object) -> str:
        raise ProcessExecutionError(["pueue", "kill"], 1, "", "no such task")

    monkeypatch.setattr(services.pueue, "kill", raise_it)
    manager.stop("vllm")  # does not raise
    with pytest.raises(LookupError):
        manager.cache.service("vllm")


# --- Services.status ---


def test_status_probes_the_named_service(
    monkeypatch: pytest.MonkeyPatch, manager: Services
) -> None:
    """`status(name)` probes exactly that service's tunneled URL."""
    manager.cache.save_service(make_service("vllm", local_port=8000, health_path="/health"))
    monkeypatch.setattr(
        services, "probe_health", lambda url: url == "http://localhost:8000/health"
    )
    [status] = manager.status("vllm")
    assert status.healthy is True
    assert status.record.name == "vllm"


def test_status_lists_every_service_when_name_omitted(
    monkeypatch: pytest.MonkeyPatch, manager: Services
) -> None:
    """`status()` with no name reports every recorded service."""
    manager.cache.save_service(make_service("a", local_port=1))
    manager.cache.save_service(make_service("b", local_port=2))
    monkeypatch.setattr(services, "probe_health", lambda url: False)
    statuses = manager.status()
    assert {status.record.name for status in statuses} == {"a", "b"}


# --- Services.logs ---


def test_logs_prints_the_captured_log(
    monkeypatch: pytest.MonkeyPatch, wired: RecordingMachine, manager: Services, capsys
) -> None:
    """A plain `logs` call prints the remote task's captured output."""
    manager.cache.save_service(make_service("vllm", remote_task="3", root="/root"))
    monkeypatch.setattr(services.pueue, "log", lambda task_id, **kwargs: f"log for {task_id}")
    manager.logs("vllm")
    assert "log for 3" in capsys.readouterr().out


def test_logs_follow_streams_via_pueue_follow(
    monkeypatch: pytest.MonkeyPatch, wired: RecordingMachine, manager: Services
) -> None:
    """`--follow` runs `pueue follow <task>` in the foreground instead of a one-shot `log`."""
    manager.cache.save_service(make_service("vllm", remote_task="3", root="/root"))
    manager.logs("vllm", follow=True)
    assert wired.calls[-1] == ["pueue", "follow", "3"]
