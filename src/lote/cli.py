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
    lote status   [target] [--all] [--verbose]  # jobs: recent across hosts, or a host's live jobs
    lote monitor  [targets...] [--interval S] [--fetch PATH]  # live multi-host view
    lote monitor  --once [--json]               # one durable pass: harvest finished jobs, exit
    lote reconcile <target>                     # compare local run state with the scheduler
    lote interact <target> [--gpus N] [--hours H] [--dry-run]
    lote serve start  <name> <target> --cmd "<cmd>" --port N [--local-port N] [--health-path P]
    lote serve stop   <name>                        # kill the remote task + tunnel, drop record
    lote serve status [name]                        # health of one service, or every service
    lote serve logs   <name> [--follow]              # the service's captured log
    lote logs     <target> <handle> [--follow]
    lote cancel   <target> <handle>             # qdel / scancel / pueue state-aware cancel
    lote kill     <target> <handle>             # alias for cancel
    lote revive   <target>                      # restart a dead pueue daemon (pueued -d)
    lote info     <target> <handle>             # post-mortem (exit code, mem, GPU)
    lote poll     <target> <handle>             # one bounded probe, verdict -> exit code
    lote fetch    <target> <path>               # rsync a results path back
    lote pull     <handle>                      # rsync back the run's recorded path
    lote watch    <target>                      # re-sync on every local file change
    lote history  [limit]                       # recent lote command history
    lote exec ...                               # the on-host executor (run/qsub/sbatch/...)
"""

import functools
import os
import shlex
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from json import dumps
from pathlib import Path
from time import monotonic, sleep
from typing import Concatenate

from cyclopts import App
from plumbum import FG, ProcessExecutionError, SshMachine
from watchfiles import watch as watch_files

from . import CONFIG, NAME, STATE_DIR
from .cache import Cache, RunRecord
from .dispatch import Dispatcher, connect
from .executor.cli import JobArg, handled, project_group
from .executor.cli import app as exec_app
from .history import History
from .jobspec import JobSpec
from .log import logger
from .models import Config, NodeClass, Target
from .monitor import DownHost, Failed, Finished, MonitorReport
from .nodes import PROBE_WAIT, PROBE_WALLTIME, parse_snapshot, probe_spec, wait_for
from .reconcile import ReconcileRow
from .render import Renderer
from .schedulers import (
    HostUnreachable,
    JobState,
    Resources,
    Scheduler,
    failure_reason,
    log_excerpt,
    pick,
    read_log,
    short_reason,
    verdict_line,
)
from .services import Services
from .sync import GitignoreFilter
from .targets import find_root, probe_capabilities, resolve, smallest_fit, ssh_hosts
from .watcher import single_watcher

# how many of the newest runs per host `lote status` shows before `--all`: enough to see what is
# in flight and just finished, without walking a cache full of old dead jobs.
RECENT_RUNS = 8


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


def run_tty(command: list[str], dry_run: bool) -> None:
    """Run ``command`` on a real TTY (ssh needs a pty), or print it under ``--dry-run``."""
    if dry_run:
        print(shlex.join(command))
        return
    subprocess.run(command, check=False)


def row(
    state: JobState, *, script: str = "", submitted_at: str = "", name: str = ""
) -> ReconcileRow:
    """A reconcile / post-mortem row from a scheduler ``JobState``."""
    return ReconcileRow(
        handle=state.handle,
        script=script,
        submitted_at=submitted_at,
        name=name or state.label or "",
        state=state.state,
        exit_code=state.exit_code,
        verdict=state.verdict,
    )


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
    def _dispatch(self) -> Dispatcher:
        """The CLI-free submit core, sharing the CLI's config and cache.

        Every dispatch path the CLI exposes (``submit``/``run``) and the programmatic
        ``Dispatcher.run``/``await_many`` an experiment framework calls go through this one
        object, so the command line and a caller can never drift.
        """
        return Dispatcher(config=self._config, cache=self._cache)

    @functools.cached_property
    def _services(self) -> Services:
        """The CLI-free ``serve`` core, sharing the CLI's cache so a service outlives the process
        that started it."""
        return Services(cache=self._cache)

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
    def discover(self, target: str, wait: float = PROBE_WAIT) -> None:
        """Onboard ``target`` and map its node classes, then show what it is.

        Onboarding is probe + sync + ``chefe install``, then one minimal
        mainboard job submitted to each queue the scheduler enumerates
        (``qstat -q`` / ``sinfo``), so the cache learns what every class of
        node -- the login node, each compute/GPU queue, special movers like
        Miyabi's ``prepost`` -- actually has.

        wait: seconds to wait for each queue's probe job before skipping that class.
        """
        self._render.targets([(target, self._onboard(target, wait=wait))])

    @recorded
    def setup(self, target: str, wait: float = PROBE_WAIT) -> None:
        """Onboard ``target``: probe, sync the repo, install the env, start the queue.

        wait: seconds to wait for each queue's probe job before skipping that class.
        """
        self._onboard(target, wait=wait)
        logger.info("setup complete on {}", target)

    @recorded
    def submit(
        self,
        target: str,
        script: str = "",
        *args: JobArg,
        cmd: str = "",
        queue: str | None = None,
        walltime: str | None = None,
        gpus: int = 0,
        account: str = "",
        mem: int | None = None,
        name: str = "",
        pythonpath: str = "",
        needs: float | None = None,
        fetch: str | None = None,
        targets: str | None = None,
    ) -> str:
        """rsync the repo to ``target`` (``auto`` routes by ``--needs``) and run a job.

        Two ways to say what to run, both ending in the same scheduler dispatch:

        - **a script** -- ``lote submit <target> worker.sh [args]`` submits an
          existing ``.sh``. A concrete local path is staged and shipped through
          ``.lote/jobs``. A bare name stays bare for in-repository experiment lookup.
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
        queue/gpus: scheduler overrides. A generated job defaults to ``debug-g`` when queue is
            unset. An existing script keeps its ``#PBS -q`` or ``#SBATCH --partition`` value.
        walltime: ``HH:MM:SS`` cap for a generated job. Unset means the PBS default header on a
            cluster and NO cap on a schedulerless (pueue/bash) host; the dispatcher logs the
            effective value either way, so the cap is never silent.
        account: the PBS ``group_list`` for a generated job, when the probe missed it.
        mem: system memory in GB to request, so a memory-hungry job gets the headroom it needs
            (PBS ``mem=NNgb`` in the select chunk, SLURM ``--mem=NNG``) instead of an OOM kill.
        pythonpath: explicit ``PYTHONPATH`` for the job, empty for a clean environment.
        """
        spec = (
            JobSpec(
                cmd=cmd,
                queue=queue or "debug-g",
                walltime=walltime,
                gpus=gpus,
                account=account,
                mem_gb=mem,
                pythonpath=pythonpath,
            )
            if cmd
            else None
        )
        resources = Resources(
            gpus=gpus,
            queue=queue,
            walltime=walltime,
            account=account or None,
            mem_gb=mem,
        )
        if targets is not None:
            handles = ",".join(
                self._submit_one(
                    a,
                    script,
                    args,
                    spec=spec,
                    resources=resources,
                    needs=needs,
                    fetch=fetch,
                    name=name,
                )
                for a in _split_targets(targets)
            )
        else:
            handles = self._submit_one(
                target,
                script,
                args,
                spec=spec,
                resources=resources,
                needs=needs,
                fetch=fetch,
                name=name,
            )
        return handles  # the CLI boundary prints the returned handles, exactly once

    def _submit_one(
        self,
        target: str,
        script: str,
        args: Sequence[str],
        *,
        spec: JobSpec | None,
        resources: Resources,
        needs: float | None,
        fetch: str | None,
        name: str = "",
    ) -> str:
        """Dispatch a job to one resolved target and record the run (the submit core).

        Both paths run through the shared :class:`Dispatcher`, so the command line and a
        programmatic caller can never drift. A ``--cmd`` job (``spec`` set) goes through
        :meth:`Dispatcher.run`, which generates the script, derives the scheduler request, and
        records the run in one place; an existing-script job goes through :meth:`Dispatcher.submit`
        with CLI resource overrides. Fields left unset continue to come from the script's
        ``#PBS`` or ``#SBATCH`` directives. That chokepoint stages every concrete local script
        before any scheduler can see it.
        """
        if target == "auto":
            if needs is None:
                raise SystemExit("`--needs <GB>` is required when target is `auto`")
            machine = smallest_fit(self._known_targets(), float(needs))
        else:
            machine = self.target(target)
        if spec is not None:
            return self._dispatch.run(
                machine,
                spec.cmd,
                gpus=spec.gpus,
                queue=spec.queue,
                walltime=spec.walltime,
                account=spec.account,
                mem_gb=spec.mem_gb,
                pythonpath=spec.pythonpath,
                fetch=fetch,
                name=name,
            ).id
        return self._dispatch.submit(
            machine, script, args, resources=resources, fetch=fetch, name=name
        )

    @recorded
    def run(
        self,
        target: str,
        command: str = "",
        *,
        file: str = "",
        detach: bool = False,
        queue: str = "",
        walltime: str | None = None,
        gpus: int = 0,
        account: str = "",
        mem: int | None = None,
        hours: int = 2,
        pythonpath: str = "",
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
        queue/gpus: PBS/SLURM header values for the generated job script
            (a batch job defaults to ``debug-g``; an interactive session keeps the
            probed queue unless ``--queue`` is given).
        walltime: ``HH:MM:SS`` cap. Unset means the PBS default header on a cluster and NO cap
            on a schedulerless host (the old silent 30-minute default killed healthy runs);
            the effective value is logged at submit.
        account: the PBS ``group_list`` for the generated job, when the probe missed it.
        mem: system memory in GB to request for the generated job (PBS ``mem=NNgb`` / SLURM
            ``--mem=NNG``), so a memory-hungry run is not OOM-killed.
        hours: walltime in hours for an interactive shell (when no command is given).
        pythonpath: explicit ``PYTHONPATH`` for the job, empty for a clean environment.
        """
        machine = self.target(target)
        if not command and not file:
            self._shell(machine, gpus=gpus or 1, hours=hours, queue=queue, account=account)
            return None
        if file:
            remote_file = f".lote/run-{Path(file).name}"
            destination = f"{machine.name}:{machine.root}/{remote_file}"
            self._config.ssh.copy(file, destination, host=machine.name)
            command = f"python {shlex.quote(remote_file)}"
        spec = JobSpec(
            cmd=command,
            queue=queue or "debug-g",
            walltime=walltime,
            gpus=gpus,
            account=account,
            mem_gb=mem,
            pythonpath=pythonpath,
        )
        handle = self._submit_one(
            machine.name,
            "",
            (),
            spec=spec,
            resources=Resources(),
            needs=None,
            fetch=None,
        )
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
    def status(
        self, target: str | None = None, *, all: bool = False, verbose: bool = False
    ) -> None:
        """Show jobs. The one job-listing command (``ps`` folded in).

        ``lote status`` (no target) renders one table across every host of your recent runs and
        their live outcome. ``lote status <target>`` asks that host's scheduler for the jobs it is
        running right now (pueue/PBS/SLURM alike); stop one with ``lote cancel <target> <handle>``.

        The ``Status`` column is lote's single normalized outcome -- ``running`` / ``ok`` /
        ``failed`` / ``vanished`` / ``unreachable`` (the host could not be probed this pass, a dead
        ssh link or a downed scheduler daemon, so its jobs are retried next time and never crash
        the table) -- the same words on every backend. ``--verbose`` also shows the scheduler's own
        raw state code (PBS ``R``/``F``, pueue ``Running``/``Done``) behind it.
        Only recent runs show by default; ``--all`` walks the full history (slower on a big cache).
        """
        if target is not None:
            machine = self._observe(target)
            with connect(machine.name) as remote:
                states = pick(machine).jobs(remote, machine.root)
            self._render.states(target, states, verbose=verbose)
            return
        self._render.jobs(self._job_rows(self._targets(), all=all), verbose=verbose)

    def _job_rows(
        self, aliases: list[str], *, all: bool = False
    ) -> list[tuple[str, ReconcileRow]]:
        """Resolve cached runs on each onboarded ``alias`` against its scheduler.

        Returns ``(alias, row)`` pairs — the structured feed shared by the no-arg ``status`` table
        and the ``monitor`` loop. By default every still-in-flight run shows (however many there
        are) plus only the most recent finished runs per host, so a cache full of old dead jobs
        never clutters the table yet a wide fan-out is never hidden; ``all`` includes the whole
        history. A finished job's verdict never changes, so it is read straight from the cache;
        only the still-live runs touch the host, in one batched ``states`` query.
        """
        rows: list[tuple[str, ReconcileRow]] = []
        with self._render.spinner("resolving jobs") as spin:
            for alias in aliases:
                cached = self._cached(alias)
                if cached is None:
                    continue
                runs = [r for r in self._cache.recent(limit=200) if r.target == alias]
                if not runs:
                    continue
                spin.update(f"resolving {alias} ({len(runs)} runs)")
                resolved = self._resolve(cached, runs)
                if not all:  # keep every in-flight run, but only the latest few finished ones
                    live = [r for r in resolved if r.verdict == "running"]
                    done = [r for r in resolved if r.verdict != "running"][:RECENT_RUNS]
                    resolved = live + done
                rows.extend((alias, r) for r in resolved)
        return rows

    def _resolve(self, cached: Target, runs: list[RunRecord]) -> list[ReconcileRow]:
        """Resolve a host's runs to rows, probing it only for the ones not already terminal.

        A run with a terminal verdict cached is rendered from the cache (no network). The rest are
        resolved by one batched ``states`` call -- which already carries finished history on PBS
        and pueue -- and any handle that call misses (a finished SLURM job, say) gets a single
        ``state`` probe. Every freshly resolved verdict is written back, a cache read next time.

        When that probe fails (a dead ssh link or a downed scheduler daemon raises
        :class:`HostUnreachable`), this host's pending runs become ``unreachable`` rows with the
        reason rather than crashing the whole status, so the other hosts still render and the jobs
        here are simply retried next pass.
        """
        pending = [r for r in runs if (r.verdict or "running") == "running"]
        if not pending:
            return [self._run_row(run, {}) for run in runs]
        try:
            live = self._probe_host(cached, pending)
        except HostUnreachable as down:
            reason = str(down) or "unreachable"
            pending_handles = {run.handle for run in pending}
            return [
                self._unreachable_row(run, reason)
                if run.handle in pending_handles
                else self._run_row(run, {})
                for run in runs
            ]
        return [self._run_row(run, live) for run in runs]

    def _probe_host(self, cached: Target, pending: list[RunRecord]) -> dict[str, JobState]:
        """Probe one host for its pending runs, writing each freshly resolved verdict to the cache.

        One batched ``states`` round-trip resolves the live runs; a handle that batch misses (a
        finished SLURM job) gets one direct ``state``. Raises :class:`HostUnreachable` (an ssh
        transport fault, or a dead daemon as :class:`~.schedulers.DaemonDown`) so :meth:`_resolve`
        can mark this host unreachable and move on instead of letting it crash the whole status.
        """
        scheduler = pick(cached)
        with connect(cached.name) as remote:
            live = scheduler.states(remote, cached.root, [run.handle for run in pending])
            for run in pending:
                found = live.get(run.handle)
                if found is None:  # absent from the batch (finished SLURM): one direct probe
                    found = scheduler.state(remote, cached.root, run.handle)
                self._cache.resolve(run, found.state, found.exit_code, found.verdict)
                live[run.handle] = found
        return live

    def _unreachable_row(self, run: RunRecord, reason: str) -> ReconcileRow:
        """A row for a run on a host this pass could not probe, carrying why in ``state``.

        The ``unreachable`` verdict is distinct from a settled outcome, so one dead host never
        crashes status and its jobs are retried next pass; the reason rides in ``state`` for the
        durable monitor to surface per host and for ``lote revive`` to act on.
        """
        return ReconcileRow(
            handle=run.handle,
            script=run.script,
            submitted_at=run.submitted_at,
            name=run.name,
            state=reason,
            verdict="unreachable",
        )

    def _run_row(self, run: RunRecord, live: dict[str, JobState]) -> ReconcileRow:
        """A row for one run: its freshly probed live state if present, else the cached verdict."""
        if (found := live.get(run.handle)) is not None:
            return row(found, script=run.script, submitted_at=run.submitted_at, name=run.name)
        return ReconcileRow(
            handle=run.handle,
            script=run.script,
            submitted_at=run.submitted_at,
            name=run.name,
            state=run.state,
            exit_code=run.exit_code,
            verdict=run.verdict or "vanished",
        )

    @recorded
    def monitor(
        self,
        *targets: str,
        interval: float = 10.0,
        fetch: str | None = None,
        once: bool = False,
        json: bool = False,
    ) -> None:
        """Watch jobs across hosts, as a live view or one durable, harness-friendly pass.

        Default (blocking, ctrl-c to stop): a refresh-in-place view. Every ``interval`` seconds it
        resolves each target's jobs (the same robust feed as ``lote status``) and, when ``--fetch
        PATH`` is given, rsyncs that results path back and counts the ``part-*.parquet`` shards
        under it, so one table shows both job state and experiment progress.

        ``--once``: one non-blocking pass for a periodic harness cron. It resolves every tracked
        job across all hosts once (robust, so one dead host never crashes it), auto-pulls the
        results of any job that reached a terminal verdict since the last pass, records that
        verdict so the next ``--once`` reports only new changes, and returns. With ``--json`` it
        prints a structured summary to stdout -- ``{running, finished, failed,
        unreachable_hosts, changed}`` -- for the cron to act on; otherwise it logs the counts.
        Idempotent and fast, it survives the agent that dispatched the jobs: a finished or failed
        remote job is harvested by whatever runs the next sweep, never lost because the watcher
        died with its turn.

        targets: target aliases to watch; defaults to every onboarded host.
        interval: seconds between refreshes (the live view only).
        fetch: a results path (relative to the repo root) to rsync back and tally parquet parts
            each live tick; lote stays research-agnostic, counting ``part-*.parquet`` generically.
        once: do a single durable pass and exit, instead of the blocking live view.
        json: with ``--once``, print the structured summary as JSON on stdout.
        """
        aliases = list(targets) or self._targets()
        if once:
            report = self._sweep(aliases)
            if json:
                print(dumps({**report.model_dump(), "changed": report.changed}))
            else:
                logger.info(
                    "sweep: {} running, {} finished, {} failed, {} unreachable host(s) "
                    "(changed={})",
                    report.running,
                    len(report.finished),
                    len(report.failed),
                    len(report.unreachable_hosts),
                    report.changed,
                )
            return
        with self._render.live() as live:
            try:
                while True:
                    jobs = self._job_rows(aliases)
                    progress = self._fetch_progress(aliases, fetch) if fetch else None
                    live.update(self._render.monitor(jobs, progress, path=fetch))
                    sleep(interval)
            except KeyboardInterrupt:
                logger.info("monitor stopped")

    def _sweep(self, aliases: list[str]) -> MonitorReport:
        """Resolve every tracked job once, harvest the newly terminal ones, report the changes.

        The durable counterpart of the live loop, sharing its one-pass resolver
        (:meth:`_job_rows`, robust per host). Each job is classified by its verdict: a
        still-``running`` one is counted, an ``unreachable`` host is noted once with its reason,
        and a job that reached a terminal verdict the monitor has not yet reported is harvested
        (its results pulled if it finished ok, the verdict recorded so the next sweep stays
        silent). Idempotent, so a harness cron can call it on a schedule and a second pass with
        nothing new reports ``changed=false``.
        """
        running = 0
        finished: list[Finished] = []
        failed: list[Failed] = []
        down: dict[str, str] = {}
        for alias, item in self._job_rows(aliases, all=True):
            if item.verdict == "running":
                running += 1
            elif item.verdict == "unreachable":
                down.setdefault(alias, item.state or "unreachable")
            elif self._cache.run(item.handle, target=alias).reported != item.verdict:
                self._harvest(alias, item, finished, failed)
        return MonitorReport(
            running=running,
            finished=finished,
            failed=failed,
            unreachable_hosts=[
                DownHost(host=host, reason=reason) for host, reason in down.items()
            ],
        )

    def _harvest(
        self, alias: str, item: ReconcileRow, finished: list[Finished], failed: list[Failed]
    ) -> None:
        """Record a newly terminal job's verdict and, when it finished ok, pull its results back.

        Appends to the ``finished``/``failed`` accumulators in place so :meth:`_sweep` reads as one
        classify loop, then advances the run's reported cursor so the same outcome is never
        announced twice.
        """
        run = self._cache.run(item.handle, target=alias)
        if item.verdict == "ok":
            finished.append(
                Finished(handle=item.handle, target=alias, pulled_path=self._auto_pull(run))
            )
        else:  # failed or vanished: surface the cause, there is nothing to pull back
            reason = short_reason(item.verdict, item.exit_code)
            failed.append(Failed(handle=item.handle, target=alias, reason=reason))
        self._cache.report(run, item.verdict)

    def _auto_pull(self, run: RunRecord) -> str | None:
        """Pull a finished run's recorded results path back and return it, or None if nothing came.

        None when the run carried no ``--fetch`` path, or the pull itself failed (a host-only
        results dir that never got written), so a missing artifact never crashes a sweep.
        """
        if not run.fetch_path:
            return None
        try:
            self._fetch(run.target, run.fetch_path)
        except ProcessExecutionError as error:
            logger.warning("could not pull {} from {}: {}", run.fetch_path, run.target, error)
            return None
        return run.fetch_path

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
        machine = self._observe(target)
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
        self._shell(self.target(target), gpus=gpus, hours=hours, dry_run=dry_run)

    @recorded
    def serve_start(
        self,
        name: str,
        target: str,
        cmd: str,
        *,
        port: int,
        local_port: int | None = None,
        health_path: str = "/health",
        timeout: float = 300.0,
    ) -> None:
        """Launch ``cmd`` on ``target`` as a persistent service named ``name``, tunneled here.

        The remote side is a ``pueue`` task -- exactly what any dispatched job gets, so the
        service survives a dropped ssh link and is inspectable with ``serve logs``/``status``
        and stoppable with ``serve stop``. The local side is a second, self-reconnecting
        ``ssh -L`` tunnel, itself a local pueue task, reachable at ``http://localhost:<port>``
        once it answers ``health_path``.

        name: this service's key for later ``stop``/``status``/``logs`` calls.
        target: the lote target alias to launch on.
        cmd: the shell command to run (a full invocation -- activate its own venv if needed).
        port: the port ``cmd`` binds on ``target``.
        local_port: the local port to tunnel it to; defaults to ``port``.
        health_path: the HTTP path polled to decide the service is up.
        timeout: seconds to wait for a healthy response before returning (the task keeps
            running regardless -- a slow model load is not a failure).
        """
        machine = self.target(target)
        outcome = self._services.start(
            name,
            machine,
            cmd,
            port=port,
            local_port=local_port,
            health_path=health_path,
            timeout=timeout,
        )
        url = f"http://localhost:{outcome.record.local_port}"
        if outcome.healthy:
            logger.info(
                "{} healthy at {} (remote task {}, tunnel task {})",
                name,
                url,
                outcome.record.remote_task,
                outcome.record.tunnel_task,
            )
        else:
            logger.warning(
                "{} tunneled at {} but did not answer {} within {}s; `lote serve logs {}` "
                "to check what it's doing",
                name,
                url,
                health_path,
                timeout,
                name,
            )

    @recorded
    def serve_stop(self, name: str) -> None:
        """Stop service ``name``: kill its remote task and local tunnel, drop the record."""
        record = self._services.stop(name)
        logger.info("stopped {} on {}", name, record.target)

    @recorded
    def serve_status(self, name: str | None = None) -> None:
        """Show ``name``'s live health, or every recorded service when ``name`` is omitted."""
        self._render.services(self._services.status(name))

    @recorded
    def serve_logs(self, name: str, follow: bool = False) -> None:
        """Print (``--follow`` to stream) the captured log of service ``name``'s remote task."""
        self._services.logs(name, follow=follow)

    @recorded
    def logs(self, target: str, handle: str, follow: bool = False) -> None:
        """Print the run log for ``handle`` on ``target``.

        follow: stream the log as it grows and return once the job reaches a
            terminal state, instead of printing what is captured so far.
        """
        machine = self._observe(target)
        scheduler = pick(machine)
        with connect(machine.name) as remote:
            if follow:
                scheduler.stream(remote, machine.root, handle)
            else:
                scheduler.logs(remote, machine.root, handle)

    @recorded
    def why(self, target: str, handle: str) -> None:
        """Explain ``handle`` on ``target``: one structured verdict line, then the log tail.

        The triage shortcut. The first line is always the verdict -- ``<handle> <verdict>
        (exit N, <decoded reason>, submitted <age>)`` -- so a script or a skimming eye reads
        the outcome before any log content. A non-ok terminal job adds the extracted one-line
        cause (a raised exception, a scheduler rejection, or the decoded signal exit), and the
        last meaningful log lines follow with rich panel borders and ANSI noise stripped, so
        the excerpt is never a wall of box-drawing glyphs. ``lote status`` shows the verdict
        table; ``lote why`` shows the story of one job.
        """
        machine = self._observe(target)
        scheduler = pick(machine)
        with connect(machine.name) as remote:
            state = scheduler.state(remote, machine.root, handle)
            log = read_log(remote, machine.root, handle)
        submitted_age = ""
        with suppress(LookupError):  # an unrecorded handle still gets its verdict
            submitted_age = self._render.when(
                self._cache.run(handle, target=machine.name).submitted_at
            )
        print(verdict_line(state, submitted_age=submitted_age))
        if state.verdict not in {"ok", "running"}:
            print(f"reason: {failure_reason(log, state.exit_code)}")
        if tail := log_excerpt(log):
            print("log tail:")
            for line in tail:
                print(f"  {line}")

    def wait(self, target: str, handle: str) -> None:
        """Block until ``handle`` on ``target`` reaches a terminal state, then report and exit.

        Prints ``<handle> done: ok`` on success, or ``<handle> <verdict>: <reason>`` and exits
        non-zero on failure. The watcher form: background it (a background task) so the moment a
        remote PBS/Slurm job ends or fails you are woken with the cause inline, instead of polling
        ``lote status`` by hand. One watcher per handle gives one event per job.
        """
        machine = self._observe(target)
        scheduler = pick(machine)
        try:
            with single_watcher(handle), connect(machine.name) as remote:
                state = scheduler.wait(remote, machine.root, handle)
                log = "" if state.verdict == "ok" else read_log(remote, machine.root, handle)
        except HostUnreachable as down:  # host down past the retry budget, not a job verdict
            logger.info(f"{handle} unreachable: {down}")
            raise SystemExit(2) from None
        reason = "" if state.verdict == "ok" else failure_reason(log, state.exit_code)
        if state.verdict == "ok":
            logger.info(f"{handle} done: ok")
            return
        logger.info(f"{handle} {state.verdict}: {reason}")
        raise SystemExit(1)

    @recorded
    def cancel(self, target: str, handle: str) -> None:
        """Cancel job ``handle`` on ``target`` through its scheduler's valid operation."""
        machine = self._observe(target)
        with connect(machine.name) as remote:
            pick(machine).cancel(remote, machine.root, handle)

    @recorded
    def kill(self, target: str, handle: str) -> None:
        """Alias for ``cancel``: stop job ``handle`` on ``target`` on any backend."""
        self.cancel(target, handle)

    @recorded
    def revive(self, target: str) -> None:
        """Restart ``target``'s scheduler daemon so a dead pueue queue is one command to recover.

        The companion to the ``unreachable: daemon down`` verdict ``status`` now shows: when a
        host's ``pueued`` has died and every job on it reads unreachable, ``lote revive <target>``
        brings the daemon back (``pueued -d``) over ssh, then ``status`` resolves its jobs again.
        Because pueue requeues every task that was running when the daemon died, the revive also
        clears those zombies (their real process is gone), so the host comes back with an honest
        job table and the monitor stops counting phantoms. A site-managed cluster (PBS, SLURM) has
        no user daemon to revive and says so.
        """
        machine = self.target(target)
        with connect(machine.name) as remote:
            cleared = pick(machine).revive(remote, machine.root)
        if cleared:
            logger.info(
                "revived {} and cleared {} zombie task(s): {}",
                target,
                len(cleared),
                ", ".join(cleared),
            )
        else:
            logger.info("revived the scheduler daemon on {}", target)

    @recorded
    def info(self, target: str, handle: str) -> None:
        """Show a job's post-mortem (PBS: exit status, mem used vs cap, GPU usage)."""
        machine = self._observe(target)
        with connect(machine.name) as remote:
            state = pick(machine).state(remote, machine.root, handle)
        self._render.reconcile([row(state)])

    @recorded
    def poll(self, target: str, handle: str) -> None:
        """One bounded probe of ``handle``: print ``<handle> <verdict>`` and exit by verdict.

        The watcher primitive. Unlike ``wait`` (which holds one ssh session open for the job's
        whole life and so dies on MaxSessions, a link blip past ~60s, or scheduler GC), this opens
        one ssh, queries once, persists the verdict to the local cache so later pueue/PBS history
        GC cannot turn a finished ``ok`` into ``vanished``, and returns. A backgrounded ``lote
        poll`` therefore exits promptly and triggers a clean harness notification; re-run it on a
        ~90s schedule until the verdict is terminal. The exit code is the verdict, so a script can
        branch without parsing: 0 ok, 1 failed, 2 still running, 3 vanished/unknown, 4 unreachable.
        """
        machine = self._observe(target)
        scheduler = pick(machine)
        try:
            with connect(machine.name) as remote:
                state = scheduler.state(remote, machine.root, handle)
        except HostUnreachable as down:  # a transient blip; the next scheduled poll simply retries
            logger.info(f"{handle} unreachable: {down}")
            raise SystemExit(4) from None
        with suppress(LookupError):  # persist before GC erases it (no-op for an unrecorded handle)
            run = self._cache.run(handle, target=machine.name)
            self._cache.resolve(run, state.state, state.exit_code, state.verdict)
        suffix = "" if state.exit_code is None else f" exit={state.exit_code}"
        lifecycle = (
            str(state.state).casefold()
            if state.state in {"Locked", "Stashed", "Queued", "Paused"}
            else state.verdict
        )
        logger.info(f"{handle} {lifecycle}{suffix}")
        raise SystemExit({"ok": 0, "failed": 1, "running": 2}.get(state.verdict, 3))

    @recorded
    def fetch(self, target: str, path: str) -> None:
        """rsync ``path`` (relative to the repo root) back from ``target``."""
        self._fetch(target, path)

    @recorded
    def pull(self, handle: str, target: str = "") -> None:
        """rsync back the results path recorded for ``handle`` at submit time.

        ``target`` disambiguates a handle that several hosts have issued (pueue ids are
        small reused integers); a handle recorded on one host alone needs no target.
        """
        run = self._cache.run(handle, target=target or None)
        if not run.fetch_path:
            raise SystemExit(
                f"run {handle!r} has no fetch path; use `lote fetch {run.target} <path>`"
            )
        self._fetch(run.target, run.fetch_path)

    @recorded
    def watch(self, target: str) -> None:
        """Re-sync the repo to ``target`` on every local file change (ctrl-c to stop)."""
        machine = self.target(target)
        self._dispatch.rsync_up(machine)
        logger.info("watching repo -> {} (ctrl-c to stop)", machine.name)
        for changes in watch_files(*self._config.sync.include):
            shipped = [path for _, path in changes if not self._sync.ignored(path)]
            if shipped:
                self._dispatch.rsync_up(machine)
                logger.info("re-synced after {} change(s)", len(shipped))

    @recorded
    def history(self, limit: int = 20) -> None:
        """Show the most recent ``lote`` command invocations."""
        self._render.history(self._history.recent(limit))

    def _targets(self) -> list[str]:
        """Target aliases: ``lote.toml`` overrides, else ``~/.ssh/config``."""
        return self._config.targets or ssh_hosts()

    def target(self, alias: str) -> Target:
        """The onboarded :class:`Target` for ``alias``, probing and caching the host on first use.

        The public alias-to-:class:`Target` resolver a programmatic caller uses to hand a named
        host to :class:`~.dispatch.Dispatcher`, so a script never reaches into a private resolver.
        """
        return self._cached(alias) or self._onboard(alias)

    def _observe(self, alias: str) -> Target:
        """Resolve ``alias`` for a read-only verb: cached facts, else a bare probe.

        ``status``/``why``/``logs``/``info``/``poll``/``wait``/``cancel``/``fetch`` only read a
        host, so an alias the cache has never seen gets one ssh probe (scheduler, root) and
        nothing else -- no rsync, no ``chefe install``, and therefore no ``[sync]`` requirement.
        The full :meth:`target` onboarding stays the dispatch path's concern; the probe result is
        deliberately not cached, since only a host that can build the env may enter the lote.
        """
        if (cached := self._cached(alias)) is not None:
            return cached
        with connect(alias) as remote:
            facts = probe_capabilities(remote, alias)
        return resolve(alias, self._config, facts)

    def _cached(self, alias: str) -> Target | None:
        """Resolve ``alias`` from cached facts only; None if never onboarded."""
        facts = self._cache.facts(alias)
        return resolve(alias, self._config, facts) if facts is not None else None

    def _known_targets(self) -> list[Target]:
        """Onboarded targets (known VRAM) — the candidates for ``submit auto``."""
        return [target for alias in self._targets() if (target := self._cached(alias))]

    def _onboard(self, alias: str, *, wait: float = PROBE_WAIT) -> Target:
        """Find the root, rsync, ``chefe install``, probe the login node, then every queue.

        Cached only once ``chefe install`` succeeds: ``setup.sh`` runs under
        ``set -e``, so a failed install raises before ``save_facts`` — a machine
        that can't build the env never enters the lote. Queue classes are cached
        one by one as their probe jobs report back, so an interrupted discover
        keeps the classes that already landed.

        wait: seconds to wait for each queue's probe job before skipping that class.
        """
        setup = (Path(__file__).parent / "scripts" / "setup.sh").read_text()
        with connect(alias) as remote:
            root = find_root(remote)
            self._dispatch.rsync_up(Target(name=alias, root=root))
            remote["bash"][["-c", setup, "lote-setup", root]] & FG
            facts = probe_capabilities(remote, alias)
            machine = resolve(alias, self._config, facts)
            self._cache.save_facts(alias, machine)
            classes = dict(machine.classes)
            for node in self._probe_queues(machine, remote, wait=wait):
                self._cache.save_node(alias, node)
                classes[node.name] = node
        machine = machine.model_copy(update={"classes": classes})
        self._cache.save_facts(alias, machine)
        logger.info(
            "onboarded {} ({}, {}, {} node class(es))",
            alias,
            machine.kind,
            machine.root,
            len(machine.classes),
        )
        return machine

    def _probe_queues(
        self, machine: Target, remote: SshMachine, *, wait: float
    ) -> Iterator[NodeClass]:
        """One :class:`NodeClass` per scheduler queue that answers the mainboard probe.

        The queue list comes from the scheduler itself (PBS ``qstat -q``, SLURM
        ``sinfo``), so special classes like Miyabi's ``prepost`` movers are found,
        never configured. An ssh host has no queues and yields nothing, keeping its
        onboarding exactly the single login-node probe it always was.
        """
        scheduler = pick(machine)
        for queue in scheduler.queues(remote, machine.root):
            node = self._probe_queue(scheduler, machine, remote, queue, wait=wait)
            if node is not None:
                yield node

    def _probe_queue(
        self, scheduler: Scheduler, machine: Target, remote: SshMachine, queue: str, *, wait: float
    ) -> NodeClass | None:
        """Probe one queue with a minimal submitted job; None when the class is unreachable.

        The generated script prints mainboard's machine snapshot on a node of the
        queue. A rejected submit, a deadline hit, a failed job, or a log without a
        snapshot is a warning and a skipped class, never a failed discover.
        """
        script = self._dispatch.write_job_script(machine, probe_spec(queue))
        self._dispatch.rsync_up(machine, extra=(script,))
        resources = Resources(queue=queue, walltime=PROBE_WALLTIME)
        try:
            handle = scheduler.submit(remote, machine.root, script, (), resources=resources)
        except SystemExit as error:
            logger.warning("queue {} rejected the probe job ({}); class skipped", queue, error)
            return None
        logger.info("probing queue {} with job {}", queue, handle)
        final = wait_for(scheduler, remote, machine.root, handle, timeout=wait)
        if final.verdict != "ok":
            logger.warning(
                "probe job {} on queue {} ended {}; class skipped", handle, queue, final.verdict
            )
            return None
        try:
            return parse_snapshot(queue, read_log(remote, machine.root, handle))
        except LookupError as error:
            logger.warning("{}; class skipped", error)
            return None

    def _fetch(self, target: str, path: str) -> None:
        """rsync ``path`` back from ``target`` into the same local path."""
        self._dispatch.fetch_path(self._observe(target), path)

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


def build_serve_app(lote: Lote) -> App:
    """The ``lote serve`` subapp: start/stop/status/logs for a persistent tunneled service.

    Mirrors ``exec_app``'s mounted-subapp shape (its own small ``App``, wired with
    ``handled`` at each command) rather than adding ``serve_*`` verbs to the flat top-level
    namespace, so ``lote serve --help`` reads as one coherent feature.
    """
    app = App(
        name="serve",
        help="Manage persistent services: a supervised remote process tunneled to a local port.",
    )
    app.command(handled(lote.serve_start), name="start")
    app.command(handled(lote.serve_stop), name="stop")
    app.command(handled(lote.serve_status), name="status")
    app.command(handled(lote.serve_logs), name="logs")
    return app


def build(lote: Lote) -> App:
    """Wire ``lote``'s commands into the cyclopts app, mounting ``lote exec``/``lote serve``.

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
    app.command(build_serve_app(lote))
    app.command(handled(lote.ls))
    app.command(handled(lote.probe))
    app.command(handled(lote.discover))
    app.command(handled(lote.setup))
    app.command(handled(lote.submit))
    app.command(handled(lote.run))
    app.command(handled(lote.status))
    app.command(handled(lote.monitor))
    app.command(handled(lote.reconcile))
    app.command(handled(lote.interact))
    app.command(handled(lote.logs))
    app.command(handled(lote.why))
    app.command(handled(lote.wait))
    app.command(handled(lote.cancel))
    app.command(handled(lote.kill))
    app.command(handled(lote.revive))
    app.command(handled(lote.info))
    app.command(handled(lote.poll))
    app.command(handled(lote.fetch))
    app.command(handled(lote.pull))
    app.command(handled(lote.watch))
    app.command(handled(lote.history))
    return app


app = build(Lote())


def chdir_root() -> None:
    """Run from the nearest lote root, walking up from the cwd the way ``git`` does.

    The root is the first ancestor holding a ``lote.toml`` or a ``.lote/`` state dir. Every
    lote path (the config, the SQLite cache, ``[sync].include``, fetch paths) is root-relative,
    so anchoring the process here lets ``lote status``/``why``/``monitor`` run from any
    subdirectory instead of failing with a misleading "nothing to sync" after silently creating
    an empty cache in the wrong place. A tree with no root yet keeps the cwd (the fresh-repo
    case, where the user is about to write ``lote.toml``).
    """
    for candidate in (cwd := Path.cwd(), *cwd.parents):
        if (candidate / CONFIG).is_file() or (candidate / STATE_DIR).is_dir():
            if candidate != cwd:
                os.chdir(candidate)
                logger.debug("running from the lote root {}", candidate)
            return


def main() -> None:
    """The console entry point: anchor at the lote root, then run the CLI."""
    chdir_root()
    app()


if __name__ == "__main__":
    main()
