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
from math import ceil
from pathlib import Path
from time import sleep

import pendulum
from plumbum import ProcessExecutionError, SshMachine

from .base import FrozenModel
from .cache import Cache, RunRecord
from .clients.rsync import Rsync, rsync
from .environment import Environment
from .jobspec import JobSpec
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
from .sync import GitignoreFilter, SyncLock
from .targets import smallest_fit
from .transport import SshTransport

# How a finished verdict maps to a process exit code, the same contract `lote poll` exposes:
# 0 ok, 1 failed, 2 still running, 3 vanished/unknown. A caller can branch on this without
# re-deriving it, and `await_many` carries it on each `Verdict`.
VERDICT_EXITS = {"ok": 0, "failed": 1, "running": 2}

# Chefe owns these generated files as one indivisible input pair. Lote does not inspect their
# contents. It only makes them required sync sources so a host receives the same manifest and lock
# that were validated locally, even though the workspace intentionally ignores `.chefe/` in Git.
_CHEFE_COMPILED_PAIR = (".chefe/pixi.toml", ".chefe/pixi.lock")
_CHEFE_INCLUDE_FILTERS = ("/.chefe/", *[f"/{path}" for path in _CHEFE_COMPILED_PAIR])
_CHEFE_REMAINDER_FILTER = "/.chefe/***"


def connect(name: str, ssh: SshTransport | None = None) -> SshMachine:
    """Open an ssh connection to ``name`` with the user install dirs on PATH.

    The single source of the bare-tool PATH (chefe/pueue/nvidia-smi), shared with the CLI's
    own ``connect`` so a job dispatched programmatically rides the exact same activated session.
    """
    return Environment(root=".", ssh=ssh or Config().ssh).connection(name)


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
        """Hash by ``(target, handle)`` so a Handle keys an await map across hosts.

        The carried :class:`Target` holds an unhashable ``classes`` dict, so the field-derived hash
        a frozen pydantic model would build does not apply. The handle id alone is not enough: two
        pueue hosts count task ids independently, so a fan-out can hold the same id on two targets.
        """
        return hash((self.target.name, self.id))

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
        pythonpath: str = "",
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
        walltime: ``HH:MM:SS`` cap; None means the PBS default header on a cluster and no cap
            on a schedulerless (pueue/bash) host -- the effective value is always logged.
        gpus/queue/account/mem_gb/pythonpath: the generated job's scheduler request.
        fetch: a results path recorded on the handle, pulled back by :meth:`fetch`.
        name: a human label stored with the run.
        known_targets: the ``auto`` routing candidates (the caller's onboarded hosts).
        """
        machine = self._route(target, needs_gb, known_targets)
        spec = JobSpec(
            cmd=cmd,
            queue=queue,
            walltime=walltime,
            gpus=gpus,
            account=account,
            mem_gb=mem_gb,
            pythonpath=pythonpath,
        )
        self._log_walltime(machine, spec)
        script = self.write_job_script(machine, spec)
        resources = Resources(
            gpus=spec.gpus,
            walltime=spec.walltime,
            queue=spec.queue,
            account=spec.account or None,
            mem_gb=spec.mem_gb,
        )
        # `submit` stages every concrete local script under `.lote/jobs/` and ships it before the
        # scheduler handoff. The PBS header bakes the request in, while the SLURM backend applies
        # `resources` as `sbatch` overrides, so neither backend silently drops it.
        handle = self.submit(machine, script, (), resources=resources, fetch=fetch, name=name)
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
    ) -> str:
        """Ship the repo, dispatch ``script`` to ``machine``'s scheduler, record the run, hand back
        the handle.

        The one submit chokepoint both the CLI and :meth:`run` go through. Every concrete local
        script is content-addressed under ``.lote/jobs`` and synchronously shipped before the
        scheduler sees its repository-relative path. Bare names remain bare for the executor's
        experiment lookup. ``resources`` carries the request the SLURM backend applies as
        ``sbatch`` overrides, and the recorded :class:`RunRecord` captures the git sha so
        ``status`` and ``pull`` resolve it later.
        """
        prepared, required = self._prepare_script(script)
        self.rsync_up(machine, extra=required)
        sha = git("rev-parse", "--short", "HEAD")
        dirty = bool(git("status", "--porcelain"))
        with self._connection(machine.name) as remote:
            self._verify_chefe(remote, machine)
            try:
                handle = pick(machine).submit(
                    remote, machine.root, prepared, args, resources=resources
                )
            except SystemExit as error:
                raise SystemExit(
                    f"submission to target {machine.name!r} failed. {error}"
                ) from None
        self.cache.record(
            RunRecord(
                handle=handle,
                target=machine.name,
                kind=machine.kind,
                script=prepared,
                args=" ".join(shlex.quote(a) for a in args),
                git_sha=sha,
                dirty=int(dirty),
                submitted_at=pendulum.now().to_iso8601_string(),
                fetch_path=fetch,
                name=name,
            )
        )
        logger.info(
            "{} -> {} on {} ({}{})",
            prepared,
            handle,
            machine.name,
            sha,
            "+dirty" if dirty else "",
        )
        return handle

    def _verify_chefe(self, remote: SshMachine, machine: Target) -> None:
        """Fail fast, in one plain sentence, when ``machine``'s chefe cannot run.

        Every dispatch reaches the host through ``chefe run lote exec ...``
        (:meth:`Environment.wrap`), so a broken remote chefe -- a stale editable install, a
        dependency its venv never picked up, a source path an over-eager ``.gitignore`` entry
        silently dropped from sync -- used to surface only as a raw Python traceback buried
        inside the *job's* captured log, naming whatever module chefe happened to die
        importing rather than the real host-provisioning problem. This runs the same activated
        ``chefe --help`` every job depends on and turns a nonzero exit into a clear diagnosis
        before the scheduler ever sees the job.
        """
        body = Environment(root=machine.root).wrap("chefe --help", chefe=False)
        retcode, _, err = remote["bash"][["-lc", body]].run(retcode=None)
        if retcode != 0:
            raise SystemExit(
                f"chefe on {machine.name!r} is broken ({failure_reason(err)}). "
                f"Run `lote setup {machine.name}` to reinstall it from the synced source."
            )

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

    def _log_walltime(self, machine: Target, spec: JobSpec) -> None:
        """State the effective walltime at submit, so the cap (or its absence) is never silent.

        The 30-minute JobSpec default once killed three healthy gold runs without a word; now a
        PBS submit names the header value and whether it was defaulted, and a schedulerless
        submit names its enforced cap or says the run is uncapped.
        """
        if machine.kind == "pbs":
            source = "explicit" if spec.walltime else "PBS default header"
            logger.info("walltime {} ({})", spec.pbs_walltime, source)
        elif spec.walltime:
            logger.info(
                "walltime {} (enforced by timeout on this schedulerless host)", spec.walltime
            )
        else:
            logger.info("no walltime cap (schedulerless host; pass a walltime to bound the run)")

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
            with self._connection(handle.alias) as remote:
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
            run = self.cache.run(handle.id, target=handle.alias)
            self.cache.resolve(run, state.state, state.exit_code, state.verdict)
        if state.verdict == "ok":
            return Verdict(verdict="ok", exit_code=state.exit_code)
        with self._connection(handle.alias) as remote:
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

    def _prepare_script(self, script: str) -> tuple[str, tuple[str, ...]]:
        """Stage a concrete local script and return its host-safe path plus required sync source.

        Bare names stay unchanged so the executor can search in-repository experiment jobs. An
        explicit path must exist locally, since forwarding an unresolved local path would make the
        host fail later with no way for lote to guarantee what it runs.
        """
        source = Path(script).expanduser()
        try:
            content = source.read_bytes()
        except FileNotFoundError as error:
            if Path(script).name == script:
                return script, ()
            raise FileNotFoundError(
                f"cannot submit script {script!r}: the local file does not exist, so lote "
                "cannot ship it to the host"
            ) from error
        digest = hashlib.sha256(content).hexdigest()[:12]
        staged = Path(".lote") / "jobs" / f"job-{digest}.sh"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        path = str(staged)
        return path, (path,)

    def rsync_up(self, machine: Target, *, extra: Sequence[str] = ()) -> None:
        """Mirror the repo to ``machine``; git-ignored files and the denylist skipped.

        Repository and nested ``.gitignore`` files are the primary send and delete boundary.
        ``[sync].protect`` is the escape hatch for remote-only artifacts outside that boundary.
        ``extra`` ships paths outside the sync allowlist that must still reach the host. Chefe's
        compiled ``pixi.toml`` and ``pixi.lock`` always ride as one required pair despite the Git
        ignore, while the rest of ``.chefe`` remains excluded. Fails fast when no include paths
        are declared, the compiled pair is incomplete, or chefe installs an unshipped dep. A
        stale include path that no longer exists locally is dropped with one clear warning.
        """
        if not self.config.sync.include:
            raise LookupError(
                "nothing to sync. Declare include paths under [sync] in lote.toml "
                "before dispatching jobs"
            )
        include = [path for path in self.config.sync.include if Path(path).exists()]
        if stale := [path for path in self.config.sync.include if path not in include]:
            logger.warning(
                "skipping {} stale [sync].include path(s) missing locally: {} "
                "(remove them from lote.toml)",
                len(stale),
                ", ".join(stale),
            )
        if not include:
            raise LookupError(
                "every [sync].include path is missing locally; fix lote.toml before dispatching"
            )
        chefe_manifest = Path("chefe.toml")
        compiled_pair = tuple(path for path in _CHEFE_COMPILED_PAIR if Path(path).is_file())
        if chefe_manifest.is_file() and compiled_pair != _CHEFE_COMPILED_PAIR:
            raise LookupError(
                "chefe.toml requires the compiled `.chefe/pixi.toml` and `.chefe/pixi.lock` "
                "pair before remote sync. Run `chefe install --resolve` on this solve-capable "
                "machine, then dispatch again."
            )
        missing = uncovered_path_deps(chefe_manifest, include)
        if missing:
            raise LookupError(
                f"chefe.toml installs editable path deps not shipped by [sync].include: "
                f"{', '.join(missing)}. Add them under [sync] in lote.toml so `chefe install` "
                "can build the env on the host."
            )
        gitignore_files = self.sync.control_files(include)
        required = (*compiled_pair, *extra)
        if compiled_pair == _CHEFE_COMPILED_PAIR:
            self._warn_env_swap(machine)
        with SyncLock(machine.name, self.sync.root):
            try:
                rsync(
                    [*include, *gitignore_files, *required],
                    f"{machine.name}:{machine.root}/",
                    Rsync.ARCHIVE
                    | Rsync.COMPRESS
                    | Rsync.RELATIVE
                    | Rsync.VERBOSE
                    | Rsync.DELETE
                    | Rsync.DELETE_AFTER,
                    include=_CHEFE_INCLUDE_FILTERS if compiled_pair else (),
                    filters=self.sync.filters,
                    exclude=[
                        *([_CHEFE_REMAINDER_FILTER] if compiled_pair else []),
                        *self.sync.excludes,
                        *self.config.sync.exclude,
                    ],
                    protect=self.config.sync.protect,
                    rsh=self.config.ssh.rsync_shell,
                    timeout=ceil(self.config.ssh.deadline),
                    host=machine.name,
                    allow_vanished=not gitignore_files and not required,
                )
            except ProcessExecutionError as error:
                if not required:
                    raise
                paths = ", ".join(required)
                raise RuntimeError(
                    f"failed to ship required sync path {paths} to {machine.name}; "
                    "submission aborted before scheduler dispatch"
                ) from error

    def _warn_env_swap(self, machine: Target) -> None:
        """Warn when this sync would rewrite the host's env under its running jobs.

        The first ``chefe run`` after a sync that changes the compiled lock rebuilds the
        remote env in place, and several sessions dispatch to one box, so a submit from one
        screen can swap site-packages under a job another screen still has mid-run. The
        submit proceeds -- queueing more work on a busy host is the normal flow -- but the
        operator is told exactly which running jobs now sit on a shifting env.
        """
        lock = Path(".chefe/pixi.lock")
        local = hashlib.sha256(lock.read_bytes()).hexdigest()
        try:
            with self._connection(machine.name) as remote:
                retcode, out, _ = remote["sha256sum"][f"{machine.root}/{lock}"].run(retcode=None)
                if retcode != 0 or out.startswith(local):  # no remote env yet, or same lock
                    return
                states = pick(machine).jobs(remote, machine.root)
        except HostUnreachable:
            return  # the sync itself surfaces the transport fault with its own clear error
        if running := [job.handle for job in states if job.verdict == "running"]:
            logger.warning(
                "this sync changes .chefe/pixi.lock on {} while job(s) {} run there; "
                "the next chefe run rebuilds the env under them",
                machine.name,
                ", ".join(running),
            )

    def fetch_path(self, machine: Target, path: str) -> None:
        """rsync ``path`` back from ``machine`` into the same local path.

        The path-addressed counterpart of :meth:`fetch`: the CLI knows the host and a results path
        directly (``lote fetch``/``pull``/the monitor tick), so it pulls without a :class:`Handle`.
        Works for either a file or a directory. The remote item is pulled into its parent without a
        trailing slash, so a single-file path no longer turns into a local directory.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rsync(
            [f"{machine.name}:{machine.root}/{target}"],
            f"{target.parent}/",
            rsh=self.config.ssh.rsync_shell,
            timeout=ceil(self.config.ssh.deadline),
            host=machine.name,
        )
        logger.info("fetched {} from {}", path, machine.name)

    def _connection(self, name: str) -> SshMachine:
        """Open one SSH session under this dispatcher's configured bounded policy."""
        return connect(name, self.config.ssh)
