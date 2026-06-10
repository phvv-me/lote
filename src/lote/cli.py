"""Dispatch jobs from the laptop to a lote machine and pull results back.

Thin control plane that drives the on-host ``lote exec`` executor over SSH.
Config lives in ``lote.toml``; state (host discovery + the run registry +
command history) lives in one WAL-mode SQLite ``.lote/db.sqlite``. Hosts are
onboarded once (``lote setup``): probe + rsync + ``chefe install``, so only
machines that can build the env enter the lote. Each command is timed and recorded
to ``.lote/db.sqlite`` + ``.lote/lote.log`` (opt out with ``LOTE_NO_HISTORY=1``).

- Transport: ``lote.clients.rsync`` ships the repo; ``plumbum.SshMachine`` runs
  remote commands (honouring ``~/.ssh/config``, one reused connection per call).
- ``ssh`` targets (DGX Spark, PCs): jobs go to ``pueue``.
- ``pbs``/``slurm`` targets (HPC): jobs go to ``lote exec qsub``/``sbatch``.

Subcommands::

    lote ls                                     # targets + cached capabilities
    lote probe    <target>                      # preview a host over ssh, no sync/install
    lote discover <target>                      # onboard: probe + sync + `chefe install`
    lote setup    <target>                      # same, and start the pueue daemon
    lote submit   <target|auto> <script> [args] [--needs GB] [--fetch PATH]
    lote submit   --targets a,b   <script> [args] [--needs GB] [--fetch PATH]
    lote run      <target> "<cmd>" [--detach] [--gpus N]  # queue + stream a command
    lote ps       [target]                      # one host's live jobs, else recent runs
    lote status   <target>                      # live jobs on a target
    lote monitor  [targets...] [--interval S] [--fetch PATH]  # live multi-host view
    lote reconcile <target>                     # compare local run state with the scheduler
    lote interact <target> [--gpus N] [--hours H] [--dry-run]
    lote logs     <target> <handle> [--follow]
    lote cancel   <target> <handle>             # qdel / scancel / pueue kill
    lote kill     <target> <handle>             # alias for cancel
    lote info     <target> <handle>             # post-mortem (exit code, mem, GPU)
    lote fetch    <target> <path>               # rsync a results path back
    lote pull     <handle>                      # rsync back the run's recorded path
    lote watch    <target>                      # re-sync on every local file change
    lote history  [limit]                       # recent lote command history
    lote exec ...                               # the on-host executor (run/qsub/sbatch/...)
"""

import functools
import hashlib
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from time import monotonic, sleep
from typing import Concatenate

import pendulum
from cyclopts import App
from plumbum import FG, ProcessExecutionError, SshMachine
from watchfiles import watch as watch_files

from . import NAME
from .cache import Cache, RunRecord
from .clients.rsync import Rsync, rsync
from .environment import Environment
from .executor.cli import JobArg, handled, project_group
from .executor.cli import app as exec_app
from .history import History
from .jobspec import DEFAULT_PYTHONPATH, JobSpec
from .log import logger
from .models import Config, Target
from .reconcile import ReconcileRow
from .render import Renderer
from .schedulers import JobState, Resources, pick
from .sync import GitignoreFilter
from .targets import find_root, probe_capabilities, resolve, smallest_fit, ssh_hosts


def recorded[**P, R](
    command: Callable[Concatenate[Lote, P], R],
    /,
) -> Callable[Concatenate[Lote, P], R]:
    """Decorator: time the command and append it to the CLI's history (ok or error)."""

    @functools.wraps(command)
    def wrapper(self: Lote, /, *args: P.args, **kwargs: P.kwargs) -> R:
        started = monotonic()
        # collect the scalar positionals as ``CommandArg`` for the audit log (the
        # first string is the target the command acted on).
        recorded_args = [arg for arg in args if isinstance(arg, str | int | float | None)]
        name = command.__name__
        try:
            result = command(self, *args, **kwargs)
        except BaseException as error:  # record, then let the CLI boundary report it
            self._history.record(name, recorded_args, started, "error", detail=repr(error))
            raise
        handle = result if isinstance(result, str) else None
        self._history.record(name, recorded_args, started, "ok", handle=handle)
        return result

    return wrapper


def connect(name: str) -> SshMachine:
    """Open an ssh connection to ``name`` with the user install dirs on PATH.

    Thin wrapper over :meth:`Environment.connection` -- the single source of the
    bare-tool PATH, so ``chefe``/``pueue``/``nvidia-smi`` resolve from the same
    ``user_bins`` activated commands use, never the forbidden pixi binary.
    """
    return Environment(root=".").connection(name)


def run_tty(command: list[str], dry_run: bool) -> None:
    """Run ``command`` on a real TTY (ssh needs a pty), or print it under ``--dry-run``."""
    if dry_run:
        print(shlex.join(command))
        return
    subprocess.run(command, check=False)


def row(state: JobState, *, script: str = "", submitted_at: str = "") -> ReconcileRow:
    """A reconcile / post-mortem row from a scheduler ``JobState``."""
    return ReconcileRow(
        handle=state.handle,
        script=script,
        submitted_at=submitted_at,
        state=state.state,
        exit_code=state.exit_code,
        verdict=state.verdict,
    )


def git(*args: str) -> str:
    """Stripped stdout of a local ``git`` command."""
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()


def _split_targets(targets: str) -> list[str]:
    """Parse a ``--targets`` value into aliases, accepting commas or whitespace.

    ``"gold, miyabi"`` and ``"gold miyabi"`` both fan a multi-host ``submit`` across
    the two hosts; empty fragments are dropped so a trailing comma is harmless.
    """
    return [alias for alias in targets.replace(",", " ").split() if alias]


class Lote:
    """The ``lote`` CLI: onboard hosts, dispatch jobs, pull results back.

    Control-plane state (config, cache, history) is lazy, so the on-host
    ``lote exec`` subcommands never read them on a bare remote with no
    ``lote.toml`` or ``.lote/``.
    """

    @functools.cached_property
    def _config(self) -> Config:
        return Config()

    @functools.cached_property
    def _sync(self) -> GitignoreFilter:
        return GitignoreFilter()

    @functools.cached_property
    def _cache(self) -> Cache:
        return Cache()

    @functools.cached_property
    def _render(self) -> Renderer:
        return Renderer()

    @functools.cached_property
    def _history(self) -> History:
        history = History()
        if history.enabled:
            logger.add(
                history.path.with_name(f"{NAME}.log"),
                level="DEBUG",
                format="{time:YYYY-MM-DD HH:mm:ss} {level: <8} {message}",
                rotation="2 MB",
                retention=5,
            )
        return history

    @recorded
    def ls(self) -> None:
        """List ssh-config targets with their cached capabilities (never probes)."""
        self._render.targets([(alias, self._cached(alias)) for alias in self._targets()])

    @recorded
    def probe(self, target: str) -> None:
        """Preview ``target``'s live capabilities over ssh, without syncing or installing.

        A read-only look at what a host is (scheduler, GPU, memory, root) before
        committing to ``setup``; nothing is shipped and nothing is cached.
        """
        with connect(target) as remote:
            facts = probe_capabilities(remote, target)
        self._render.targets([(target, resolve(target, self._config, facts))])

    @recorded
    def discover(self, target: str) -> None:
        """Onboard ``target`` (probe + sync + ``chefe install``) and show what it is."""
        self._render.targets([(target, self._onboard(target))])

    @recorded
    def setup(self, target: str) -> None:
        """Onboard ``target``: probe, sync the repo, install the env, start the queue."""
        self._onboard(target)
        logger.info("setup complete on {}", target)

    @recorded
    def submit(
        self,
        target: str,
        script: str = "",
        *args: JobArg,
        cmd: str = "",
        queue: str = "debug-g",
        walltime: str = "00:30:00",
        gpus: int = 0,
        account: str = "",
        pythonpath: str = DEFAULT_PYTHONPATH,
        needs: float | None = None,
        fetch: str | None = None,
        targets: str | None = None,
    ) -> str:
        """rsync the repo to ``target`` (``auto`` routes by ``--needs``) and run a job.

        Two ways to say what to run, both ending in the same scheduler dispatch:

        - **a script** -- ``lote submit <target> worker.sh [args]`` submits an
          existing ``.sh`` (the original path, unchanged).
        - **a command** -- ``lote submit <target> --cmd "python -m foo --model X"``
          GENERATES the job script (PBS header from ``--queue``/``--walltime``/
          ``--gpus``, then ``source .chefe/activate.sh; <cmd>``) and submits it, so no
          ``worker.sh`` is needed. On a PBS host it renders a PBS script; on any other
          host a plain bash wrapper. The whole environment (HPC modules + pixi env +
          PYTHONPATH) comes from chefe's ``.chefe/activate.sh``.

        With ``--targets a,b,c`` the same job fans out across several hosts in one
        call (the ``target`` positional is then ignored -- pass any placeholder),
        printing the comma-joined handles. This replaces the hand-rolled
        ``for host in ...; do lote submit $host ...; done`` launch loop.

        cmd: a command to wrap in a generated job script instead of passing a script.
        queue/walltime/gpus: PBS header values for a generated job.
        account: the PBS ``group_list`` for a generated job, when the probe missed it.
        pythonpath: ``PYTHONPATH`` for the activate.sh-absent fallback.
        """
        spec = (
            JobSpec(
                cmd=cmd,
                queue=queue,
                walltime=walltime,
                gpus=gpus,
                account=account,
                pythonpath=pythonpath,
            )
            if cmd
            else None
        )
        if targets is not None:
            handles = ",".join(
                self._submit_one(alias, script, args, spec=spec, needs=needs, fetch=fetch)
                for alias in _split_targets(targets)
            )
        else:
            handles = self._submit_one(target, script, args, spec=spec, needs=needs, fetch=fetch)
        return handles  # the CLI boundary prints the returned handles, exactly once

    def _submit_one(
        self,
        target: str,
        script: str,
        args: Sequence[str],
        *,
        spec: JobSpec | None,
        needs: float | None,
        fetch: str | None,
    ) -> str:
        """Dispatch a job to one resolved target and record the run (the submit core).

        With ``spec`` set the script is generated from the command for the resolved
        host's scheduler kind and shipped before dispatch; otherwise ``script`` is an
        existing path on the host.
        """
        if target == "auto":
            if needs is None:
                raise SystemExit("`--needs <GB>` is required when target is `auto`")
            machine = smallest_fit(self._known_targets(), float(needs))
        else:
            machine = self._target(target)
        extra: tuple[str, ...] = ()
        if spec is not None:
            script = self._write_job_script(machine, spec)
            extra = (script,)
        self._rsync_up(machine, extra=extra)
        sha = git("rev-parse", "--short", "HEAD")
        dirty = bool(git("status", "--porcelain"))
        with connect(machine.name) as remote:
            handle = pick(machine).submit(
                remote, machine.root, script, args, resources=Resources()
            )
        self._cache.record(
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
            )
        )
        logger.info(
            "{} -> {} on {} ({}{})", script, handle, machine.name, sha, "+dirty" if dirty else ""
        )
        return handle

    @recorded
    def run(
        self,
        target: str,
        command: str = "",
        *,
        file: str = "",
        detach: bool = False,
        queue: str = "",
        walltime: str = "02:00:00",
        gpus: int = 0,
        account: str = "",
        hours: int = 2,
        pythonpath: str = DEFAULT_PYTHONPATH,
    ) -> str | None:
        """Run ``command`` (or a local ``--file``) on ``target`` through its scheduler.

        The synchronous counterpart to ``submit``, and the same dispatch path:
        ``run`` ships the repo, generates a job script from the command, submits it
        through the target's scheduler (so the run is queued, captured, and
        cancellable on every backend -- pueue, PBS, SLURM, bare bash alike), then
        streams the captured log to this terminal and exits with the job's code.

        With ``--detach`` it prints the run handle and exits immediately, leaving the
        job running on the host. The output is retrievable later with
        ``lote logs <target> <handle>`` and the job is stoppable with
        ``lote cancel <target> <handle>`` -- the disconnect-safe form that needs no
        hand-rolled ``nohup`` + poll.

        With no ``command`` and no ``file`` it opens an interactive session instead
        (an ssh login shell, or a PBS ``qsub -I`` allocation), folding in ``interact``.

        target: a lote target alias.
        command: one shell command string -- quote it like ``ssh host '<cmd>'``.
        file: a local script to ship and run -- copied to the host and executed as
            ``python <file>`` in the env, replacing the manual ``scp`` + ``run`` dance.
        detach: print the handle and leave the job running, instead of streaming it.
        queue/walltime/gpus: PBS/SLURM header values for the generated job script
            (a batch job defaults to ``debug-g``; an interactive session keeps the
            probed queue unless ``--queue`` is given).
        account: the PBS ``group_list`` for the generated job, when the probe missed it.
        hours: walltime in hours for an interactive shell (when no command is given).
        pythonpath: ``PYTHONPATH`` for the activate.sh-absent fallback.
        """
        machine = self._target(target)
        if not command and not file:
            self._shell(machine, gpus=gpus or 1, hours=hours, queue=queue, account=account)
            return None
        if file:
            remote_file = f".lote/run-{Path(file).name}"
            destination = f"{machine.name}:{machine.root}/{remote_file}"
            subprocess.run(["scp", file, destination], check=True)
            command = f"python {shlex.quote(remote_file)}"
        spec = JobSpec(
            cmd=command,
            queue=queue or "debug-g",
            walltime=walltime,
            gpus=gpus,
            account=account,
            pythonpath=pythonpath,
        )
        handle = self._submit_one(machine.name, "", (), spec=spec, needs=None, fetch=None)
        if detach:
            return handle  # the CLI boundary prints the returned handle, exactly once
        self._stream(machine, handle)
        return None

    def _stream(self, machine: Target, handle: str) -> None:
        """Stream ``handle``'s captured log and block until the job ends ok.

        The synchronous tail of ``run``: the scheduler relays new log content as the
        job produces it and returns the terminal state. Any non-ok verdict (failed,
        killed, vanished, unknown) becomes a ``SystemExit`` carrying the job's exit
        code, or 1 when the scheduler reported none, so ``run`` mirrors the remote
        job's fate.
        """
        scheduler = pick(machine)
        with connect(machine.name) as remote:
            final = scheduler.stream(remote, machine.root, handle)
        if final.verdict != "ok":
            raise SystemExit(final.exit_code or 1)

    @recorded
    def ps(self, target: str | None = None, limit: int = 20) -> None:
        """Show live work: per ``target`` the scheduler's own jobs, else recent runs.

        ``lote ps <target>`` asks that host's scheduler for every live/queued job it is
        running -- submitted jobs and ad-hoc ``run`` dispatches alike, since both now go
        through the queue -- as one uniform table on every backend (pueue, PBS, SLURM).
        Stop any of them with ``lote cancel <target> <handle>``.

        ``lote ps`` with no target keeps the cross-host view of the most recent
        dispatched runs from the local registry.
        """
        if target is None:
            self._render.runs(self._cache.recent(limit))
            return
        machine = self._target(target)
        with connect(machine.name) as remote:
            states = pick(machine).jobs(remote, machine.root)
        self._render.states(target, states)

    @recorded
    def status(self, target: str | None = None) -> None:
        """Live jobs on ``target``; with no target, one unified table across every target.

        ``lote status`` (no argument) walks every onboarded host, resolves each
        recent run against its scheduler, and renders a single table with a
        ``target`` column -- the at-a-glance view of everything in flight.
        """
        if target is not None:
            machine = self._target(target)
            with connect(machine.name) as remote:
                pick(machine).status(remote, machine.root)
            return
        self._render.jobs(self._job_rows(self._targets()))

    def _job_rows(self, aliases: list[str]) -> list[tuple[str, ReconcileRow]]:
        """Resolve every cached run on each onboarded ``alias`` against its scheduler.

        Walks each host with cached facts, asks its scheduler for the live state of
        every run the cache recorded there, and returns ``(alias, row)`` pairs — the
        structured feed shared by the no-arg ``status`` table and the ``monitor`` loop.
        """
        rows: list[tuple[str, ReconcileRow]] = []
        for alias in aliases:
            cached = self._cached(alias)
            if cached is None:
                continue
            runs = [r for r in self._cache.recent(limit=50) if r.target == alias]
            if not runs:
                continue
            scheduler = pick(cached)
            with connect(cached.name) as remote:
                rows.extend(
                    (
                        alias,
                        row(
                            scheduler.state(remote, cached.root, r.handle),
                            script=r.script,
                            submitted_at=r.submitted_at,
                        ),
                    )
                    for r in runs
                )
        return rows

    @recorded
    def monitor(
        self,
        *targets: str,
        interval: float = 10.0,
        fetch: str | None = None,
    ) -> None:
        """Live, refresh-in-place view of jobs across hosts (ctrl-c to stop).

        Every ``interval`` seconds: resolve each target's scheduler jobs (the same
        feed as ``lote status``) and, when ``--fetch PATH`` is given, rsync that
        results path back from each host (reusing ``fetch``) and count the
        ``part-*.parquet`` shards under it, so one combined table shows both job
        state and experiment progress. This replaces the ``submit`` fan-out loop
        followed by manual ``fetch`` + parquet-poking.

        targets: target aliases to watch; defaults to every onboarded host.
        interval: seconds between refreshes.
        fetch: a results path (relative to the repo root) to rsync back and tally
            parquet parts each tick; lote stays research-agnostic — it only counts
            ``part-*.parquet`` files generically, never importing the experiment code.
        """
        aliases = list(targets) or self._targets()
        with self._render.live() as live:
            try:
                while True:
                    jobs = self._job_rows(aliases)
                    progress = self._fetch_progress(aliases, fetch) if fetch else None
                    live.update(self._render.monitor(jobs, progress, path=fetch))
                    sleep(interval)
            except KeyboardInterrupt:
                logger.info("monitor stopped")

    def _fetch_progress(self, aliases: list[str], path: str) -> int:
        """rsync ``path`` back from each alias and tally its ``part-*.parquet`` shards.

        File-based and research-agnostic: each worker's ``RowLog`` writes one
        immutable ``part-<host>-<pid>.parquet``, so the shard count under the merged
        fetched dir is a generic, dependency-free progress signal — no import of the
        experiment code, no parquet parsing.

        Only onboarded hosts are polled (an unprobed alias would trigger a full
        onboard mid-loop), and a host where the results path does not exist yet
        simply contributes nothing this tick instead of killing the monitor.
        """
        for alias in aliases:
            if self._cached(alias) is None:
                continue
            try:
                self._fetch(alias, path)
            except ProcessExecutionError as error:
                logger.debug("nothing to fetch from {} yet ({})", alias, error)
        return sum(1 for _ in Path(path).rglob("part-*.parquet"))

    @recorded
    def reconcile(self, target: str) -> None:
        """Compare the cache's recorded runs for ``target`` with the live scheduler.

        Shows each run's live state, exit code, and a verdict (ok / failed /
        running / vanished / unknown) — the local-state debugging aid that
        replaces email.
        """
        machine = self._target(target)
        runs = [r for r in self._cache.recent(limit=1000) if r.target == machine.name]
        scheduler = pick(machine)
        with connect(machine.name) as remote:
            rows = [
                row(
                    scheduler.state(remote, machine.root, r.handle),
                    script=r.script,
                    submitted_at=r.submitted_at,
                )
                for r in runs
            ]
        self._render.reconcile(rows)

    @recorded
    def interact(self, target: str, gpus: int = 1, hours: int = 2, dry_run: bool = False) -> None:
        """Grab a sized interactive session on ``target`` (a real TTY).

        Like ``run`` with no command, but lets a PBS target size the
        interactive ``qsub -I`` allocation (``gpus``/``hours``); an ssh target
        opens a login shell.

        gpus: nodes/GPUs to request (``select=``).
        hours: requested walltime in hours.
        dry_run: print the command instead of running it.
        """
        self._shell(self._target(target), gpus=gpus, hours=hours, dry_run=dry_run)

    @recorded
    def logs(self, target: str, handle: str, follow: bool = False) -> None:
        """Print the run log for ``handle`` on ``target``.

        follow: stream the log as it grows and return once the job reaches a
            terminal state, instead of printing what is captured so far.
        """
        machine = self._target(target)
        scheduler = pick(machine)
        with connect(machine.name) as remote:
            if follow:
                scheduler.stream(remote, machine.root, handle)
            else:
                scheduler.logs(remote, machine.root, handle)

    @recorded
    def cancel(self, target: str, handle: str) -> None:
        """Cancel job ``handle`` on ``target`` (PBS ``qdel`` / Slurm ``scancel`` / pueue kill)."""
        machine = self._target(target)
        with connect(machine.name) as remote:
            pick(machine).cancel(remote, machine.root, handle)

    @recorded
    def kill(self, target: str, handle: str) -> None:
        """Alias for ``cancel``: stop job ``handle`` on ``target`` on any backend."""
        self.cancel(target, handle)

    @recorded
    def info(self, target: str, handle: str) -> None:
        """Show a job's post-mortem (PBS: exit status, mem used vs cap, GPU usage)."""
        machine = self._target(target)
        with connect(machine.name) as remote:
            state = pick(machine).state(remote, machine.root, handle)
        self._render.reconcile([row(state)])

    @recorded
    def fetch(self, target: str, path: str) -> None:
        """rsync ``path`` (relative to the repo root) back from ``target``."""
        self._fetch(target, path)

    @recorded
    def pull(self, handle: str) -> None:
        """rsync back the results path recorded for ``handle`` at submit time."""
        run = self._cache.run(handle)
        if not run.fetch_path:
            raise SystemExit(
                f"run {handle!r} has no fetch path; use `lote fetch {run.target} <path>`"
            )
        self._fetch(run.target, run.fetch_path)

    @recorded
    def watch(self, target: str) -> None:
        """Re-sync the repo to ``target`` on every local file change (ctrl-c to stop)."""
        machine = self._target(target)
        self._rsync_up(machine)
        logger.info("watching repo -> {} (ctrl-c to stop)", machine.name)
        for changes in watch_files(*self._config.sync.include):
            shipped = [path for _, path in changes if not self._sync.ignored(path)]
            if shipped:
                self._rsync_up(machine)
                logger.info("re-synced after {} change(s)", len(shipped))

    @recorded
    def history(self, limit: int = 20) -> None:
        """Show the most recent ``lote`` command invocations."""
        self._render.history(self._history.recent(limit))

    def _targets(self) -> list[str]:
        """Target aliases: ``lote.toml`` overrides, else ``~/.ssh/config``."""
        return self._config.targets or ssh_hosts()

    def _target(self, alias: str) -> Target:
        """Cached :class:`Target`, onboarding the host on first use."""
        return self._cached(alias) or self._onboard(alias)

    def _cached(self, alias: str) -> Target | None:
        """Resolve ``alias`` from cached facts only; None if never onboarded."""
        facts = self._cache.facts(alias)
        return resolve(alias, self._config, facts) if facts is not None else None

    def _known_targets(self) -> list[Target]:
        """Onboarded targets (known VRAM) — the candidates for ``submit auto``."""
        return [target for alias in self._targets() if (target := self._cached(alias))]

    def _onboard(self, alias: str) -> Target:
        """Find the root, rsync, ``chefe install``, then probe over ssh for a Target.

        Cached only once ``chefe install`` succeeds: ``setup.sh`` runs under
        ``set -e``, so a failed install raises before ``save_facts`` — a machine
        that can't build the env never enters the lote.
        """
        setup = (Path(__file__).parent / "scripts" / "setup.sh").read_text()
        with connect(alias) as remote:
            root = find_root(remote)
            self._rsync_up(Target(name=alias, root=root))
            remote["bash"][["-c", setup, "lote-setup", root]] & FG
            facts = probe_capabilities(remote, alias)
        self._cache.save_facts(alias, facts)
        machine = resolve(alias, self._config, facts)
        logger.info("onboarded {} ({}, {})", alias, machine.kind, machine.root)
        return machine

    def _write_job_script(self, machine: Target, spec: JobSpec) -> str:
        """Render ``spec`` for ``machine``, write it under ``.lote/jobs/``, return its path.

        A PBS host gets a full ``#PBS`` script; any other host a plain bash wrapper
        (the module preamble no-ops there). The file lands at ``.lote/jobs/<hash>.sh``
        relative to the repo root, so ``_rsync_up`` ships it and the remote executor
        resolves it as an ordinary path -- no ``worker.sh`` in the experiment tree.
        """
        text = spec.render(pbs=machine.kind == "pbs")
        # content-addressed: the same job text always lands on the same file, so
        # repeated runs reuse it instead of growing `.lote/jobs` unboundedly.
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        path = Path(".lote") / "jobs" / f"job-{digest}.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    def _rsync_up(self, machine: Target, *, extra: Sequence[str] = ()) -> None:
        """Ship the repo to ``machine``; git-ignored files and the lote.toml denylist skipped.

        extra: additional include paths (e.g. a generated ``.lote/jobs`` script) that
            live outside the configured sync allowlist but must reach the host.
        """
        if not self._config.sync.include:
            raise SystemExit(
                f"nothing to sync. Declare include paths under [sync] in {NAME}.toml "
                "before onboarding or dispatching jobs"
            )
        rsync(
            [*self._config.sync.include, *extra],
            f"{machine.name}:{machine.root}/",
            Rsync.ARCHIVE | Rsync.COMPRESS | Rsync.RELATIVE,
            exclude=[*self._sync.excludes, *self._config.sync.exclude],
        )

    def _fetch(self, target: str, path: str) -> None:
        """rsync ``path`` back from ``target`` into the same local path."""
        machine = self._target(target)
        Path(path).mkdir(parents=True, exist_ok=True)
        rsync([f"{machine.name}:{machine.root}/{path}/"], f"{path}/")
        logger.info("fetched {} from {}", path, machine.name)

    def _shell(
        self,
        machine: Target,
        *,
        gpus: int = 1,
        hours: int = 2,
        queue: str = "",
        account: str = "",
        dry_run: bool = False,
    ) -> None:
        """Open an interactive TTY on ``machine``: a login shell, or a PBS ``qsub -I``.

        A PBS target requests an interactive allocation sized by ``gpus``/``hours``
        with the discovered queue and account; any other target opens a login shell.
        """
        if machine.kind != "pbs":
            run_tty(["ssh", "-t", machine.name], dry_run)
            return
        flags = self._qsub_interactive(
            machine,
            gpus=gpus,
            hours=hours,
            queue=queue,
            account=account,
        )
        run_tty(["ssh", "-t", machine.name, f"bash -lc {shlex.quote(shlex.join(flags))}"], dry_run)

    def _qsub_interactive(
        self,
        machine: Target,
        *,
        gpus: int,
        hours: int,
        queue: str = "",
        account: str = "",
    ) -> list[str]:
        """The ``qsub -I`` flag list for an interactive allocation on ``machine``.

        ``queue``/``account`` override the probed values; ``group_list`` falls back
        to the project group derived from a ``/work/<group>/`` root (the user account
        the probe captures is not the project group an HPC ``group_list`` needs).
        """
        flags = ["qsub", "-I", "-l", f"select={gpus}", "-l", f"walltime={max(hours, 1):02d}:00:00"]
        chosen_queue = queue or machine.queue
        if chosen_queue:
            flags += ["-q", chosen_queue]
        group = account or project_group(machine.root) or machine.account
        if group:
            flags += ["-W", f"group_list={group}"]
        return flags


def build(lote: Lote) -> App:
    """Wire ``lote``'s commands into the cyclopts app, mounting ``lote exec``.

    Each method is wrapped by `handled` at its own call site so the type checker
    sees one concrete signature per command, and so returned handles print
    exactly once at the CLI boundary.
    """
    app = App(
        name=NAME,
        help="One command plane for your machines: "
        "dispatch jobs to any host, run them under any scheduler, pull results back.",
    )
    app.command(exec_app)
    app.command(handled(lote.ls))
    app.command(handled(lote.probe))
    app.command(handled(lote.discover))
    app.command(handled(lote.setup))
    app.command(handled(lote.submit))
    app.command(handled(lote.run))
    app.command(handled(lote.ps))
    app.command(handled(lote.status))
    app.command(handled(lote.monitor))
    app.command(handled(lote.reconcile))
    app.command(handled(lote.interact))
    app.command(handled(lote.logs))
    app.command(handled(lote.cancel))
    app.command(handled(lote.kill))
    app.command(handled(lote.info))
    app.command(handled(lote.fetch))
    app.command(handled(lote.pull))
    app.command(handled(lote.watch))
    app.command(handled(lote.history))
    return app


app = build(Lote())


if __name__ == "__main__":
    app()
