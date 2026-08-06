"""Persistent remote services: a supervised process on a host, tunneled to a local port.

``submit``/``run`` dispatch a job that finishes; a service (a vLLM server, a notebook, a
dashboard) never does. The gap is filled by reusing the exact primitive lote already trusts
for a supervised process -- a ``pueue`` task -- on both ends of the connection: the remote
task runs the server (surviving disconnect, inspectable with the same ``pueue status`` /
``log`` / ``kill`` any dispatched job gets), and a second, *local* pueue task supervises a
self-reconnecting ``ssh -L`` tunnel, so "a supervised process" means one thing everywhere in
lote instead of a bespoke tunnel daemon. :class:`Services` is :class:`~.dispatch.Dispatcher`'s
counterpart for the persistent case: a CLI-free core holding the whole
start/stop/status/logs story, so ``lote serve`` is a thin wrapper over the same object a
programmatic caller can drive directly.
"""

import shlex
import socket
from contextlib import suppress
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen

import pendulum
from plumbum import FG
from plumbum.commands.processes import ProcessExecutionError

from .base import FrozenModel
from .cache import Cache, ServiceRecord
from .clients import pueue
from .dispatch import connect
from .environment import Environment
from .models import Target

# Seconds between health probes while a service comes up, and each probe's own connect
# timeout -- short enough that a still-loading server (a multi-GB model) is polled often
# without a slow probe itself eating into the caller's overall `timeout` budget.
HEALTH_INTERVAL = 2.0

# How long a dropped tunnel connection waits before ssh reconnects, inside the local
# supervised loop -- long enough not to hammer a host that is still coming back, short
# enough that a blip heals well within a caller's next request.
RECONNECT_BACKOFF = 2

SSH_KEEPALIVE = (
    "-o",
    "ExitOnForwardFailure=yes",
    "-o",
    "ServerAliveInterval=10",
    "-o",
    "ServerAliveCountMax=3",
)


def probe_health(url: str) -> bool:
    """Whether ``url`` answers with a non-error HTTP status, swallowing every connection fault.

    The service may still be loading (model weights, a cold venv) or the tunnel may not have
    reconnected yet, so a refused connection, reset, or timeout simply means "not yet" -- never
    an error the caller must handle.
    """
    with suppress(URLError, OSError), urlopen(url, timeout=HEALTH_INTERVAL) as response:  # noqa: S310
        # `urlopen`'s stub leaves the open connection untyped, so `.status` infers as `Any`;
        # pin it to the `int` it actually returns rather than let that `Any` leak out.
        status: int = response.status
        return status < 400
    return False


def tunnel_loop(target: str, local_port: int, remote_port: int) -> str:
    """The self-reconnecting ``ssh -L`` command a local pueue task supervises.

    ``ssh -N`` opens the forward with no remote command and blocks until the link drops (a
    reboot, a network blip, ``lote serve stop`` killing the task); the surrounding ``while``
    relaunches it after :data:`RECONNECT_BACKOFF` seconds, so the tunnel outlives any single
    connection without a bespoke watchdog process -- the whole auto-restart story in one line,
    supervised the same way any lote job is.
    """
    forward = f"{local_port}:localhost:{remote_port}"
    ssh_cmd = shlex.join(["ssh", "-N", "-L", forward, *SSH_KEEPALIVE, target])
    return f"while true; do {ssh_cmd}; sleep {RECONNECT_BACKOFF}; done"


def claim_local_port(port: int) -> None:
    """Fail fast when local ``port`` is already bound, before any task is launched.

    A resident local service on the port is the silent killer: ssh binds only the other
    address family (``ExitOnForwardFailure`` never fires because one bind succeeded), and the
    health probe then answers from the resident service, reporting a healthy tunnel that goes
    nowhere. Probing with a bind up front turns that lie into a clear one-line error naming
    the fix (``--local-port``).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as taken:
            raise LookupError(
                f"local port {port} is already in use; pass --local-port to tunnel "
                f"the service to a free one"
            ) from taken


def ensure_local_daemon() -> None:
    """Start the local ``pueued`` if nothing already owns its socket, idempotently.

    Mirrors :meth:`~.schedulers.pueue.Pueue.revive`'s already-idempotent restart: ``pueued -d``
    errors when a daemon is already listening, and that failure is exactly the "already up"
    case, safely absorbed rather than treated as a launch fault.
    """
    with suppress(ProcessExecutionError):
        pueue.start()


class ServiceStatus(FrozenModel):
    """A service's record plus a live health read, the value ``serve status`` renders.

    healthy: whether the tunneled port answered ``record.health_path`` just now.
    """

    record: ServiceRecord
    healthy: bool


class Services:
    """The CLI-free core of ``lote serve``: start, stop, and inspect a persistent service.

    Holds the reusable start path (remote pueue task, local tunnel pueue task, health poll,
    record) and the stop/status/logs paths that key off a service's name alone, so a caller
    never needs to remember which host a named service lives on.
    """

    def __init__(self, cache: Cache | None = None) -> None:
        self.cache = cache or Cache()

    def start(
        self,
        name: str,
        machine: Target,
        cmd: str,
        *,
        port: int,
        local_port: int | None = None,
        health_path: str = "/health",
        timeout: float = 300.0,
    ) -> ServiceStatus:
        """Launch ``cmd`` on ``machine`` as a supervised task, tunnel it here, wait for health.

        Reads top to bottom as the whole feature: claim the local port (fail fast on a
        resident local service the health probe would otherwise mistake for the tunnel),
        submit the remote task, open the self-reconnecting local tunnel (its own supervised
        task), persist the record so a crash mid-poll still leaves ``stop``/``status`` able
        to find and tear it down, then poll the tunneled port until ``health_path`` answers
        or ``timeout`` elapses.

        name: the service's key for later ``stop``/``status``/``logs`` calls.
        machine: the already-resolved target to launch on.
        cmd: the shell command to run (a full invocation, e.g. activating its own venv).
        port: the port ``cmd`` binds on ``machine``.
        local_port: the local port to tunnel it to; defaults to ``port``.
        health_path: the HTTP path polled to decide the service is up.
        timeout: seconds to wait for a healthy response before giving up (the task is left
            running either way -- a slow model load is not a failure).
        """
        resolved_local_port = local_port or port
        claim_local_port(resolved_local_port)
        with connect(machine.name) as remote:
            remote_task = pueue.add(
                Environment(root=machine.root, login=True).wrap(cmd, chefe=False),
                machine=remote,
                root=machine.root,
                label=f"lote-serve-{name}",
                working_directory=machine.root,
            )
        ensure_local_daemon()
        tunnel_task = pueue.add(
            tunnel_loop(machine.name, resolved_local_port, port), label=f"lote-tunnel-{name}"
        )
        record = ServiceRecord(
            name=name,
            target=machine.name,
            root=machine.root,
            cmd=cmd,
            port=port,
            local_port=resolved_local_port,
            health_path=health_path,
            remote_task=remote_task,
            tunnel_task=tunnel_task,
            started_at=_now(),
        )
        self.cache.save_service(record)
        return ServiceStatus(record=record, healthy=self._await_healthy(record, timeout=timeout))

    def stop(self, name: str) -> ServiceRecord:
        """Kill both supervised tasks (remote service, local tunnel) and drop the record."""
        record = self.cache.service(name)
        with connect(record.target) as remote, suppress(ProcessExecutionError):
            pueue.kill(record.remote_task, machine=remote, root=record.root)
        with suppress(ProcessExecutionError):
            pueue.kill(record.tunnel_task)
        self.cache.remove_service(name)
        return record

    def status(self, name: str | None = None) -> list[ServiceStatus]:
        """A live health read for ``name``, or every recorded service when omitted."""
        records = [self.cache.service(name)] if name is not None else self.cache.services()
        return [self._status_of(record) for record in records]

    def logs(self, name: str, *, follow: bool = False) -> None:
        """Print (or follow) the remote task's captured log for service ``name``."""
        record = self.cache.service(name)
        with connect(record.target) as remote:
            if follow:
                pueue.binary(remote, record.root)[["follow", record.remote_task]] & FG
            else:
                print(pueue.log(record.remote_task, machine=remote, root=record.root))

    def _status_of(self, record: ServiceRecord) -> ServiceStatus:
        """One service's record paired with a fresh health probe of its tunneled port."""
        url = f"http://localhost:{record.local_port}{record.health_path}"
        return ServiceStatus(record=record, healthy=probe_health(url))

    def _await_healthy(self, record: ServiceRecord, *, timeout: float) -> bool:
        """Poll the tunneled port every :data:`HEALTH_INTERVAL` seconds until healthy.

        Gives up (returning the last probe's result) once ``timeout`` seconds have elapsed.
        """
        url = f"http://localhost:{record.local_port}{record.health_path}"
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if probe_health(url):
                return True
            sleep(HEALTH_INTERVAL)
        return probe_health(url)


def _now() -> str:
    """The current time as an ISO-8601 string, the same stamp :class:`~.cache.RunRecord` uses."""
    return pendulum.now().to_iso8601_string()
