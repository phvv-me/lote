"""The :class:`Scheduler` contract lote dispatches through.

Every job backend (pueue, PBS, SLURM, plain bash) implements this protocol so
``lote submit``/``status``/``logs``/``info``/``reconcile`` read as
orchestration: pick the target's scheduler, then delegate. New backends are new
classes, never new ``if machine.kind == ...`` branches.

A backend operates on two things: a plumbum ``remote`` machine (an open
``SshMachine`` to the host) and the repo ``root`` on that host. Methods that
need the cluster toolchain on PATH run through the host's ``jobs`` CLI in a
login shell, exactly as the standalone lote did.
"""

from time import sleep
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..base import FrozenModel
from ..environment import Environment

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ..clients.machine import Machine

# A handle is still in flight while its verdict is ``running``; every other
# verdict (``ok`` / ``failed`` / ``vanished`` / ``unknown``) is terminal.
POLL_SECONDS = 5.0


class Resources(FrozenModel):
    """A scheduler-agnostic resource request for one job.

    Each backend maps these onto its own flags (``-l select=`` for PBS,
    ``--gpus``/``--mem`` for SLURM) and ignores what it can't express.

    gpus: number of GPUs/nodes to request (0 means none, so CPU-only scripts
        run on clusters without GPU GRES).
    walltime: requested walltime as ``HH:MM:SS``, when capped.
    queue: scheduler queue/partition name.
    account: charging account / group list.
    mem_gb: system memory request in GB.
    """

    gpus: int = 0
    walltime: str | None = None
    queue: str | None = None
    account: str | None = None
    mem_gb: int | None = None


class JobState(FrozenModel):
    """A job's post-mortem state, the unit ``reconcile`` compares against the cache.

    handle: the scheduler's job handle (PBS job id, pueue task id, SLURM job id).
    label: the job's name/label, when the scheduler reports one (the live ``ps`` view).
    state: the scheduler's current state string, or None when the job vanished.
    exit_code: the process exit status, when the scheduler reports one.
    verdict: one word -- ``ok`` / ``failed`` / ``running`` / ``vanished`` /
        ``unknown`` (finished but the scheduler reported no exit status).
    """

    handle: str
    label: str | None = None
    state: str | None = None
    exit_code: int | None = None
    verdict: str


@runtime_checkable
class Scheduler(Protocol):
    """A pluggable job backend lote dispatches generically.

    ``remote`` is an open plumbum ``SshMachine`` (or ``local``); ``root`` is the
    repo path on the host. Implementations are stateless value objects, so one
    instance per kind is enough.
    """

    name: str

    def submit(
        self, remote: Machine, root: str, script: str, args: Sequence[str], *, resources: Resources
    ) -> str:
        """Launch ``script`` with ``args`` under ``resources``; return the job handle."""

    def status(self, remote: Machine, root: str) -> None:
        """Render the user's live jobs on the host (may delegate to ``jobs status``)."""

    def jobs(self, remote: Machine, root: str) -> list[JobState]:
        """List the user's live/queued jobs on the host as structured states.

        The data behind ``status``, but as values rather than a printed table, so
        the cross-backend ``ps <target>`` view (scheduler jobs plus ad-hoc runs)
        is one uniform listing on every backend.
        """

    def logs(self, remote: Machine, root: str, handle: str) -> None:
        """Print ``handle``'s captured log so far (merged stdout+stderr); see ``stream``
        for following a live job."""

    def state(self, remote: Machine, root: str, handle: str) -> JobState:
        """Post-mortem ``handle``: its state, exit code, and a verdict, for reconcile."""

    def wait(self, remote: Machine, root: str, handle: str) -> JobState:
        """Block until ``handle`` leaves the running/queued states, returning its final state.

        A backend with no queue (``Local``) already ran the job to completion, so it
        returns at once.
        """

    def stream(self, remote: Machine, root: str, handle: str) -> JobState:
        """Print ``handle``'s log as it grows until the job is terminal; return its final state.

        The synchronous half of ``lote run``: after a dispatch, block here so the CLI
        relays the captured output live and then exits with the job's code. Queued
        backends poll state + new log content; ``Pueue`` rides its native ``follow``
        (which already exits at task end).
        """

    def cancel(self, remote: Machine, root: str, handle: str) -> None:
        """Cancel ``handle`` on the host."""

    def queues(self, remote: Machine, root: str) -> list[str]:
        """The scheduler's own queue list (PBS queues, SLURM partitions).

        Each queue is a node class onboarding probes with a minimal job; a
        backend without a queue concept (pueue, bare bash) returns ``[]``.
        """


def poll_until_done(
    probe: Callable[[], JobState],
    *,
    interval: float = POLL_SECONDS,
    sleeper: Callable[[float], None] = sleep,
) -> JobState:
    """Poll ``probe`` until the returned :class:`JobState` is terminal, returning it.

    The shared body of every queued backend's :meth:`Scheduler.wait`: a job is
    terminal once its verdict leaves ``running``. ``sleeper`` is injected so a test
    drives the loop without real time passing.
    """
    while (state := probe()).verdict == "running":
        sleeper(interval)
    return state


def stream_until_done(
    probe: Callable[[], JobState],
    drain: Callable[[int], int],
    *,
    interval: float = POLL_SECONDS,
    sleeper: Callable[[float], None] = sleep,
) -> JobState:
    """Poll ``probe``, printing new log content between polls, until the job is terminal.

    The shared body of the queued backends' :meth:`Scheduler.stream`: each tick
    checks the job's state, drains whatever the log grew since the last byte
    offset, and sleeps. After the terminal state one final drain catches output
    flushed between the last tick and the job's end. ``drain(offset)`` prints the
    new content and returns how many bytes it consumed (0 while no log exists yet,
    e.g. a still-queued job).

    probe: returns the job's current :class:`JobState`.
    drain: prints log content from the given byte offset, returning the bytes read.
    interval: seconds between polls.
    sleeper: injected so a test drives the loop without real time passing.
    """
    offset = 0
    while (state := probe()).verdict == "running":
        offset += drain(offset)
        sleeper(interval)
    drain(offset)
    return state


def read_log(remote: Machine, root: str, handle: str, offset: int = 0) -> str:
    """``handle``'s captured log from byte ``offset`` on, as a string.

    Runs the on-host ``lote exec logs <handle> --offset N`` in a login shell and
    returns its stdout verbatim. The executor prints nothing when no log exists
    yet (a queued job), so this is safe to call from the first poll tick onwards.
    """
    body = Environment(root=root).exec_command("logs", handle, "--offset", str(offset))
    return str(remote["bash"][["-lc", body]](retcode=None))


def drain_log(remote: Machine, root: str, handle: str, offset: int) -> int:
    """Print ``handle``'s captured log from byte ``offset`` on; return the bytes consumed."""
    chunk = read_log(remote, root, handle, offset)
    print(chunk, end="", flush=True)
    return len(chunk.encode())
