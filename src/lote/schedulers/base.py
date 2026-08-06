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

import re
from time import sleep
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from ..base import FrozenModel
from ..environment import Environment
from ..log import logger
from ..transport import HostUnreachable, transport_failure

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ..clients.machine import Machine

# A handle is still in flight while its verdict is ``running``; every other
# verdict (``ok`` / ``failed`` / ``vanished`` / ``unknown``) is terminal.
POLL_SECONDS = 5.0
# How many consecutive unreachable probes a wait absorbs before it gives up: a refused ssh
# control-master session or a dropped link is one or two ticks, so a generous budget rides out a
# blip while a genuinely down host still surfaces after a minute of waiting, well under any real
# job's runtime.
MAX_PROBE_RETRIES = 12


def login_run(remote: Machine, body: str) -> str:
    """Run ``body`` in a login shell on ``remote`` and return its stdout.

    The single chokepoint every scheduler probe shares. It captures the ssh exit status so a
    transport failure (exit 255 with a transport phrase in stderr) raises :class:`HostUnreachable`
    instead of yielding the empty output a parser reads as a vanished job, which is exactly how a
    refused ssh session used to end a wait early. A command that genuinely ran and exited non-zero
    (qstat reporting an unknown id) returns its stdout unchanged."""
    retcode, out, err = remote["bash"][["-lc", body]].run(retcode=None)
    if transport_failure(retcode, err):
        raise HostUnreachable(err.strip()[-200:] or "ssh transport failure")
    return out


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

    def states(self, remote: Machine, root: str, handles: list[str]) -> dict[str, JobState]:
        """The state of ``handles`` (and any other live job) in one batched query, keyed by handle.

        One round-trip (``qstat -f -H <handles>`` / ``pueue status`` / ``sacct``) so ``status``
        resolves a whole host's pending runs at once instead of one ``state`` ssh per run. A handle
        the host no longer remembers is simply absent; the caller falls back to its cached verdict.
        """

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

    def revive(self, remote: Machine, root: str) -> list[str]:
        """Restart the host's scheduler daemon (pueue's ``pueued -d``), recovering a dead queue.

        The companion to the ``unreachable: daemon down`` verdict: when a backend owns a
        user-managed daemon and it died, this brings it back so jobs resolve again, returning the
        handles of any zombie tasks it had to clear on the way back (tasks the dead daemon left in
        flight whose real process is gone). A backend whose scheduler is site-managed (PBS, SLURM)
        or has no daemon (bare bash) has nothing to revive and says so, rather than silently doing
        nothing.
        """

    def queues(self, remote: Machine, root: str) -> list[str]:
        """The scheduler's own queue list (PBS queues, SLURM partitions).

        Each queue is a node class onboarding probes with a minimal job; a
        backend without a queue concept (pueue, bare bash) returns ``[]``.
        """


def resilient(
    probe: Callable[[], JobState],
    *,
    interval: float = POLL_SECONDS,
    sleeper: Callable[[float], None] = sleep,
    retries: int = MAX_PROBE_RETRIES,
) -> Callable[[], JobState]:
    """Wrap ``probe`` so a :class:`HostUnreachable` is retried instead of surfacing as a verdict.

    A transport blip (a refused ssh session, a dropped link) is not an answer about the job, so
    tenacity retries the probe at a fixed ``interval`` up to ``retries`` times before re-raising
    the original error for a genuinely down host. Only ``HostUnreachable`` is retried; a real
    JobState (``running`` included) returns at once. ``sleeper`` is injected so a test drives the
    backoff without real time passing.
    """

    def note(state: RetryCallState) -> None:
        logger.warning(f"host unreachable, retry {state.attempt_number}/{retries}")

    retrying = Retrying(
        retry=retry_if_exception_type(HostUnreachable),
        stop=stop_after_attempt(retries),
        wait=wait_fixed(interval),
        sleep=sleeper,
        reraise=True,
        before_sleep=note,
    )
    return lambda: retrying(probe)


def poll_until_done(
    probe: Callable[[], JobState],
    *,
    interval: float = POLL_SECONDS,
    sleeper: Callable[[float], None] = sleep,
    retries: int = MAX_PROBE_RETRIES,
) -> JobState:
    """Poll ``probe`` until the returned :class:`JobState` is terminal, returning it.

    The shared body of every queued backend's :meth:`Scheduler.wait`: a job is terminal once its
    verdict leaves ``running``. The probe is made :func:`resilient` first, so a transient ssh blip
    is retried rather than ending the wait on a false ``vanished``. ``sleeper`` is injected so a
    test drives the loop without real time passing.
    """
    probe = resilient(probe, interval=interval, sleeper=sleeper, retries=retries)
    while (state := probe()).verdict == "running":
        sleeper(interval)
    return state


def stream_until_done(
    probe: Callable[[], JobState],
    drain: Callable[[int], int],
    *,
    interval: float = POLL_SECONDS,
    sleeper: Callable[[float], None] = sleep,
    retries: int = MAX_PROBE_RETRIES,
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
    probe = resilient(probe, interval=interval, sleeper=sleeper, retries=retries)
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


# Failure markers in priority order: lote's own walltime-kill verdict (the wrapper writes it, so
# it is authoritative over any stale traceback in the partial output), then a raised Python
# exception (the last line of a traceback is the real cause), then a scheduler rejection, then a
# generic build/runtime error.
FAILURE_MARKERS = (
    re.compile(r"^lote: killed at walltime.*", re.MULTILINE),
    re.compile(r"^\w[\w.]*(?:Error|Exception|Interrupt|Killed)\b.*", re.MULTILINE),
    re.compile(r"^(?:qsub|sbatch|srun|pueue):.*", re.IGNORECASE | re.MULTILINE),
    re.compile(
        r"^.*(?:fatal error|error:|failed to build|No such file|out of memory|cuda error).*",
        re.IGNORECASE | re.MULTILINE,
    ),
)

# Terminal control noise a captured log carries when the job rendered rich UI: ANSI escape
# sequences, and the box-drawing/block glyphs a rich panel or table border is made of. A
# meaningful excerpt strips both, so `lote why` never quotes a panel border as the cause.
_ANSI_CODES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BOX_DRAWING = re.compile(r"[─-▟]+")

# Process exit codes that carry their own story even when the log holds no traceback, because the
# kernel or the scheduler killed the job from outside. A process killed by signal N exits 128+N, so
# 137 is SIGKILL (what the Linux OOM killer and a cgroup memory cap send, and what a hard walltime
# stop sends after its grace period) and 143 is SIGTERM (the polite walltime stop). 124 is GNU
# ``timeout``'s own "deadline reached" code, which is how the pueue/bash backend caps walltime.
# These are the exact exit-137 SIGKILLs the 14B/32B cluster jobs hit, surfaced as a clear reason.
SIGNAL_EXITS = {
    124: "timed out (walltime exceeded)",
    125: "timeout failed to start the job",
    137: "killed by SIGKILL (out of memory or walltime, exit 137)",
    139: "crashed with SIGSEGV (segfault, exit 139)",
    143: "terminated by SIGTERM (walltime or cancel, exit 143)",
}


def exit_reason(exit_code: int | None) -> str | None:
    """A human reason for an externally-imposed exit code, or None for a plain non-zero exit.

    Maps the signal-derived codes a job never raises itself (OOM/walltime SIGKILL, a SIGTERM
    walltime stop, a ``timeout`` deadline) onto one clear line, so a job the kernel killed reads as
    "out of memory or walltime" rather than the misleading last log line of work that did finish.
    """
    return SIGNAL_EXITS.get(exit_code) if exit_code is not None else None


def failure_reason(log: str, exit_code: int | None = None) -> str:
    """One-line best-effort cause of a failed job, from its captured log and exit code.

    Scans for the most telling marker in priority order (lote's own walltime-kill line, a raised
    Python exception, then a scheduler rejection, then a generic build/runtime error). A job killed
    from outside (OOM or walltime, exit 137/143/124) rarely leaves a marker, so when the log has
    none its exit code supplies the reason before the last-meaningful-line fallback. This is the
    ``logs | grep -i error | tail`` triage a person does by hand, packaged so ``lote why`` answers
    "why did it fail" in one line.
    """
    for pattern in FAILURE_MARKERS:
        if matches := pattern.findall(log):
            return matches[-1].strip()[:240]
    if reason := exit_reason(exit_code):
        return reason
    lines = meaningful_lines(log)
    return lines[-1][:240] if lines else "(no log output)"


def meaningful_lines(log: str) -> list[str]:
    """The log's content lines: ANSI codes and rich panel borders stripped, blanks dropped.

    A line inside a panel (``│ text │``) keeps its text; a pure border (``╭────╮``) vanishes,
    so an excerpt or a last-line fallback never quotes box-drawing glyphs as the cause.
    """
    stripped = (
        _BOX_DRAWING.sub(" ", _ANSI_CODES.sub("", raw)).strip() for raw in log.splitlines()
    )
    return [line for line in stripped if line]


def log_excerpt(log: str, limit: int = 10) -> list[str]:
    """The last ``limit`` meaningful log lines, the tail ``lote why`` prints under its verdict."""
    return meaningful_lines(log)[-limit:]


def short_reason(verdict: str, exit_code: int | None) -> str:
    """A short, network-free cause for a non-ok terminal verdict, from its cached state alone.

    The reason the durable monitor reports without re-reading a host's log: a vanished job says
    so, a signal exit decodes itself, a plain code reads as ``exited N``.
    """
    if verdict == "vanished":
        return "vanished (the scheduler no longer remembers the job)"
    if (known := exit_reason(exit_code)) is not None:
        return known
    if exit_code is not None:
        return f"exited {exit_code}"
    return "failed"


def verdict_line(state: JobState, *, submitted_age: str = "") -> str:
    """The one structured verdict line ``lote why`` leads with, before any log excerpt.

    ``<handle> <verdict> (exit N, <decoded reason>, submitted <age>)`` with every detail
    optional, so a running job reads ``H1 running (submitted 5 minutes ago)`` and a walltime
    kill reads ``H1 failed (exit 137, killed by SIGKILL ..., submitted 6 hours ago)``.
    """
    details: list[str] = []
    if state.exit_code is not None:
        details.append(f"exit {state.exit_code}")
        if (known := exit_reason(state.exit_code)) is not None:
            details.append(known)
    if submitted_age:
        details.append(f"submitted {submitted_age}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{state.handle} {state.verdict}{suffix}"


def drain_log(remote: Machine, root: str, handle: str, offset: int) -> int:
    """Print ``handle``'s captured log from byte ``offset`` on; return the bytes consumed."""
    chunk = read_log(remote, root, handle, offset)
    print(chunk, end="", flush=True)
    return len(chunk.encode())
