"""The programmatic submit path: dispatch a job and get a handle back, no CLI.

``lote submit`` is a cyclopts command tangled with printing, history, and
argument parsing, so a caller that wants to fan trials out to remote hosts and
await them had no clean seam. This module is that seam. :class:`Dispatcher`
holds the reusable core every dispatch shares -- rsync the repo, render a job
script, submit through the target's :class:`Scheduler`, record the run -- and
hands back a :class:`Handle` the caller can poll, await, or fetch. The
``Lote.submit`` CLI command is a thin wrapper over the same core, so the two
paths can never drift.

A caller (an experiment framework, say) writes::

    dispatcher = Dispatcher()
    handles = [dispatcher.run(host, cmd, gpus=1, fetch=out) for host, cmd in trials]
    verdicts = dispatcher.await_many(handles)   # blocks until each is terminal
    for handle in handles:
        dispatcher.fetch(handle)                # pull each trial's results back

so the whole ssh/scheduler story stays inside lote, expressed through the same
``pick``/``Scheduler`` protocol the CLI uses on pbs/slurm/pueue/bare ssh alike.
"""

import hashlib
import shlex
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from time import sleep

import pendulum
from plumbum import SshMachine

from .base import FrozenModel
from .cache import Cache, RunRecord
from .clients.rsync import Rsync, rsync
from .environment import Environment
from .jobspec import DEFAULT_PYTHONPATH, JobSpec
from .log import logger
from .models import Config, Target
from .models.config import uncovered_path_deps
from .schedulers import (
    HostUnreachable,
    JobState,
    Resources,
    failure_reason,
    pick,
    read_log,
)
from .schedulers.base import POLL_SECONDS
from .sync import GitignoreFilter
from .targets import smallest_fit

# How a finished verdict maps to a process exit code, the same contract `lote poll` exposes:
# 0 ok, 1 failed, 2 still running, 3 vanished/unknown. A caller can branch on this without
# re-deriving it, and `await_many` carries it on each `Verdict`.
VERDICT_EXITS = {"ok": 0, "failed": 1, "running": 2}


def connect(name: str) -> SshMachine:
    """Open an ssh connection to ``name`` with the user install dirs on PATH.

    The single source of the bare-tool PATH (chefe/pueue/nvidia-smi), shared with the CLI's
    own ``connect`` so a job dispatched programmatically rides the exact same activated session.
    """
    return Environment(root=".").connection(name)


def git(*args: str) -> str:
    """Stripped stdout of a local ``git`` command (the dispatched run's provenance)."""
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()


class Handle(FrozenModel):
    """A dispatched job, enough to poll, await, or fetch it without re-resolving the host.

    Returned by :meth:`Dispatcher.run`; the value a caller holds onto for the trial. It carries
    the resolved :class:`Target` so every later probe reuses the same scheduler and root the
    submit picked, never re-routing an ``auto`` job to a different host mid-flight.

    id: the scheduler's job handle (PBS job id, pueue task id, SLURM job id, the lote run id).
    target: the resolved host the job was dispatched to.
    fetch_path: the results path recorded at submit time, pulled back by :meth:`Dispatcher.fetch`.
    """

    id: str
    target: Target
    fetch_path: str | None = None

    def __hash__(self) -> int:
        """Hash by the scheduler handle, the run's unique id, so a Handle keys an await map.

        The carried :class:`Target` holds an unhashable ``classes`` dict, so the field-derived hash
        a frozen pydantic model would build does not apply; the handle id is the natural key and is
        unique per dispatched job.
        """
        return hash(self.id)

    @property
    def alias(self) -> str:
        """The host alias the job runs on."""
        return self.target.name


class Verdict(FrozenModel):
    """A terminal outcome of an awaited job, the value :meth:`Dispatcher.await_many` yields.

    verdict: one word -- ``ok`` / ``failed`` / ``vanished`` / ``unknown`` (terminal forms only).
    exit_code: the process exit status, when the scheduler reported one.
    reason: a one-line cause for a non-ok verdict (the same triage ``lote why`` prints), else "".
    """

    verdict: str
    exit_code: int | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Whether the job finished cleanly."""
        return self.verdict == "ok"

    @property
    def code(self) -> int:
        """The verdict as a process exit code (0 ok, 1 failed, 3 vanished/unknown)."""
        return VERDICT_EXITS.get(self.verdict, 3)


class Dispatcher:
    """The CLI-free core of ``lote submit``: dispatch a job and hand back a :class:`Handle`.

    Holds the reusable submit path (rsync + render + scheduler dispatch + cache record) and the
    await/fetch path (poll the scheduler until terminal, rsync results back), so a caller drives
    remote jobs as values. The :class:`Lote` CLI composes one of these and delegates to it, so the
    command line and a programmatic caller share one implementation.
    """

    def __init__(self, config: Config | None = None, cache: Cache | None = None) -> None:
        self.config = config or Config()
        self.cache = cache or Cache()
        self.sync = GitignoreFilter()

    def run(
        self,
        target: Target | str,
        cmd: str,
        *,
        needs_gb: float | None = None,
        walltime: str | None = None,
        gpus: int = 1,
        queue: str = "debug-g",
        account: str = "",
        mem_gb: int | None = None,
        pythonpath: str = DEFAULT_PYTHONPATH,
        fetch: str | None = None,
        name: str = "",
        known_targets: Sequence[Target] = (),
    ) -> Handle:
        """Dispatch ``cmd`` to ``target`` and return a :class:`Handle` to poll/await/fetch.

        Syncs the repo, generates a job script for the resolved host's scheduler, submits it, and
        records the run with its git provenance, exactly as ``lote submit --cmd`` does. ``target``
        is a resolved :class:`Target` (the programmatic caller already knows its host) or the
        string ``"auto"`` to route by ``needs_gb`` to the smallest fitting known target.

        target: the resolved host, or ``"auto"`` to size-route by ``needs_gb``.
        cmd: the command the generated job runs (``python -m experiments.x.run --shard 3``).
        needs_gb: GPU memory the trial needs, used only to route an ``"auto"`` target.
        walltime: ``HH:MM:SS`` cap; defaults to the JobSpec default when unset.
        gpus/queue/account/mem_gb/pythonpath: the generated job's scheduler request.
        fetch: a results path recorded on the handle, pulled back by :meth:`fetch`.
        name: a human label stored with the run.
        known_targets: the ``auto`` routing candidates (the caller's onboarded hosts).
        """
        machine = self._route(target, needs_gb, known_targets)
        spec = JobSpec(
            cmd=cmd,
            queue=queue,
            walltime=walltime or JobSpec.model_fields["walltime"].default,
            gpus=gpus,
            account=account,
            mem_gb=mem_gb,
            pythonpath=pythonpath,
        )
        script = self.write_job_script(machine, spec)
        resources = Resources(
            gpus=spec.gpus,
            walltime=spec.walltime,
            queue=spec.queue,
            account=spec.account or None,
            mem_gb=spec.mem_gb,
        )
        # the generated script lives under `.lote/jobs/`, outside the sync allowlist, so it rides
        # along as an `extra` path; the PBS header bakes the request in, while the SLURM backend
        # applies `resources` as `sbatch` overrides, so neither backend silently drops it.
        handle = self.submit(
            machine, script, (), resources=resources, fetch=fetch, name=name, extra=(script,)
        )
        return Handle(id=handle, target=machine, fetch_path=fetch)

    def submit(
        self,
        machine: Target,
        script: str,
        args: Sequence[str],
        *,
        resources: Resources,
        fetch: str | None = None,
        name: str = "",
        extra: Sequence[str] = (),
    ) -> str:
        """Ship the repo, dispatch ``script`` to ``machine``'s scheduler, record the run, hand back
        the handle.

        The one submit chokepoint both the CLI and :meth:`run` go through: ``extra`` ships a
        generated script that lives outside the sync allowlist, ``resources`` carries the request
        the SLURM backend applies as ``sbatch`` overrides, and the recorded :class:`RunRecord`
        captures the git sha so ``status``/``pull`` resolve it later.
        """
        self.rsync_up(machine, extra=extra)
        sha = git("rev-parse", "--short", "HEAD")
        dirty = bool(git("status", "--porcelain"))
        with connect(machine.name) as remote:
            handle = pick(machine).submit(remote, machine.root, script, args, resources=resources)
        self.cache.record(
            RunRecord(
                handle=handle,
                target=machine.name,
                kind=machine.kind,
                script=script,
                args=" ".join(shlex.quote(a) for a in args),
                git_sha=sha,
                dirty=int(dirty),
                submitted_at=pendulum.now().to_iso8601_string(),
                fetch_path=fetch,
                name=name,
            )
        )
        logger.info(
            "{} -> {} on {} ({}{})", script, handle, machine.name, sha, "+dirty" if dirty else ""
        )
        return handle

    def await_many(
        self, handles: Sequence[Handle], *, interval: float = POLL_SECONDS
    ) -> dict[Handle, Verdict]:
        """Block until every handle is terminal, returning each one's :class:`Verdict`.

        Polls the scheduler for the still-running handles each ``interval`` seconds, persisting a
        terminal verdict to the cache the moment it lands (so later scheduler GC cannot turn an
        ``ok`` into a ``vanished``) and dropping it from the poll set. A :class:`HostUnreachable`
        blip on one tick is not a verdict, so that handle is simply retried on the next tick rather
        than failing the wait. The merged map is the per-trial outcome an experiment study reads.
        """
        verdicts: dict[Handle, Verdict] = {}
        pending = list(handles)
        while pending:
            still_running: list[Handle] = []
            for handle in pending:
                resolved = self._probe(handle)
                if resolved is None or resolved.verdict == "running":
                    still_running.append(handle)
                    continue
                verdicts[handle] = self._verdict(handle, resolved)
            pending = still_running
            if pending:
                sleep(interval)
        return verdicts

    def fetch(self, handle: Handle) -> None:
        """rsync the handle's recorded results path back from its host into the same local path.

        The programmatic counterpart of ``lote pull``: a handle dispatched with ``fetch=...`` knows
        its results path, so a caller pulls one trial's output back without naming the path again.
        """
        if not handle.fetch_path:
            raise LookupError(f"handle {handle.id!r} has no fetch path to pull")
        self.fetch_path(handle.target, handle.fetch_path)

    def _route(
        self, target: Target | str, needs_gb: float | None, known_targets: Sequence[Target]
    ) -> Target:
        """Resolve ``target`` to a host: itself, or the smallest fit for an ``auto`` job."""
        if isinstance(target, Target):
            return target
        if target != "auto":
            raise LookupError(f"target must be a resolved Target or 'auto', got {target!r}")
        if needs_gb is None:
            raise LookupError("`needs_gb` is required when target is 'auto'")
        return smallest_fit(list(known_targets), float(needs_gb))

    def _probe(self, handle: Handle) -> JobState | None:
        """One scheduler probe of ``handle``; None on a transient blip the caller should retry."""
        try:
            with connect(handle.alias) as remote:
                return pick(handle.target).state(remote, handle.target.root, handle.id)
        except HostUnreachable as down:
            logger.warning("{} unreachable, retrying: {}", handle.id, down)
            return None

    def _verdict(self, handle: Handle, state: JobState) -> Verdict:
        """Persist a terminal state to the cache and project it onto a :class:`Verdict`.

        Reads the host's log for the failure reason only when the job did not end ``ok``, so a
        clean run never pays a second round-trip; a finished verdict is memoized so a re-await is
        a cache read.
        """
        with suppress(LookupError):
            run = self.cache.run(handle.id)
            self.cache.resolve(run, state.state, state.exit_code, state.verdict)
        if state.verdict == "ok":
            return Verdict(verdict="ok", exit_code=state.exit_code)
        with connect(handle.alias) as remote:
            log = read_log(remote, handle.target.root, handle.id)
        return Verdict(
            verdict=state.verdict,
            exit_code=state.exit_code,
            reason=failure_reason(log, state.exit_code),
        )

    def write_job_script(self, machine: Target, spec: JobSpec) -> str:
        """Render ``spec`` for ``machine``, write it under ``.lote/jobs/``, return its path.

        A PBS host gets a full ``#PBS`` script; any other host a plain bash wrapper. The file is
        content-addressed, so repeated runs reuse it instead of growing ``.lote/jobs`` unboundedly.
        """
        text = spec.render(pbs=machine.kind == "pbs", gpu_in_select=machine.gpu_in_select)
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        path = Path(".lote") / "jobs" / f"job-{digest}.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    def rsync_up(self, machine: Target, *, extra: Sequence[str] = ()) -> None:
        """Mirror the repo to ``machine``; git-ignored files and the denylist skipped.

        ``--delete`` prunes host paths the local tree no longer has; the ``[sync].protect``
        patterns shield remote-only artifacts (results, logs) from that pruning. ``extra`` ships
        paths outside the sync allowlist (a generated ``.lote/jobs`` script) that must still reach
        the host. Fails fast when no include paths are declared or chefe installs an unshipped dep.
        """
        if not self.config.sync.include:
            raise LookupError(
                "nothing to sync. Declare include paths under [sync] in lote.toml "
                "before dispatching jobs"
            )
        missing = uncovered_path_deps(Path("chefe.toml"), self.config.sync.include)
        if missing:
            raise LookupError(
                f"chefe.toml installs editable path deps not shipped by [sync].include: "
                f"{', '.join(missing)}. Add them under [sync] in lote.toml so `chefe install` "
                "can build the env on the host."
            )
        rsync(
            [*self.config.sync.include, *extra],
            f"{machine.name}:{machine.root}/",
            Rsync.ARCHIVE | Rsync.COMPRESS | Rsync.RELATIVE | Rsync.DELETE,
            exclude=[*self.sync.excludes, *self.config.sync.exclude],
            protect=self.config.sync.protect,
        )

    def fetch_path(self, machine: Target, path: str) -> None:
        """rsync ``path`` back from ``machine`` into the same local path.

        The path-addressed counterpart of :meth:`fetch`: the CLI knows the host and a results path
        directly (``lote fetch``/``pull``/the monitor tick), so it pulls without a :class:`Handle`.
        """
        Path(path).mkdir(parents=True, exist_ok=True)
        rsync([f"{machine.name}:{machine.root}/{path}/"], f"{path}/")
        logger.info("fetched {} from {}", path, machine.name)
