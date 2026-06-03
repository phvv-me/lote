"""Dispatch jobs from the laptop to a fleet machine and pull results back.

Thin control plane that drives the on-host ``fleet exec`` executor over SSH.
Config lives in ``fleet.toml``; state (host discovery + the run registry +
command history) lives in one TinyDB ``.fleet/db.json``. Hosts are onboarded
once (``fleet setup``): probe + rsync + ``chefe install``, so only machines that
can build the env enter the fleet. Each command is timed and recorded to
``.fleet/db.json`` + ``.fleet/fleet.log`` (opt out with ``FLEET_NO_HISTORY=1``).

- Transport: ``fleet.clients.rsync`` ships the repo; ``plumbum.SshMachine`` runs
  remote commands (honouring ``~/.ssh/config``, one reused connection per call).
- ``ssh`` targets (DGX Spark, PCs): jobs go to ``pueue``.
- ``pbs``/``slurm`` targets (HPC): jobs go to ``fleet exec qsub``/``sbatch``.

Subcommands::

    fleet ls                                     # targets + cached capabilities
    fleet discover <target>                      # onboard: probe + sync + `chefe install`
    fleet setup    <target>                      # same, and start the pueue daemon
    fleet submit   <target|auto> <script> [args] [--needs GB] [--fetch PATH]
    fleet ps                                     # recent runs across all targets
    fleet status   <target>                      # live jobs on a target
    fleet reconcile <target>                     # compare local run state with the scheduler
    fleet interact <target> [--gpus N] [--hours H] [--dry-run]
    fleet logs     <target> <handle> [--follow]
    fleet info     <target> <handle>             # post-mortem (exit code, mem, GPU)
    fleet fetch    <target> <path>               # rsync a results path back
    fleet pull     <handle>                      # rsync back the run's recorded path
    fleet watch    <target>                      # re-sync on every local file change
    fleet history  [limit]                       # recent fleet command history
    fleet exec ...                               # the on-host executor (run/qsub/sbatch/...)
"""

from __future__ import annotations

import functools
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

import fire
import pendulum
from plumbum import FG, SshMachine
from watchfiles import watch as watch_files

from .cache import Cache
from .clients.rsync import Rsync, rsync
from .executor.cli import Executor
from .history import History
from .log import logger
from .models import Config, Target
from .reconcile import ReconcileRow
from .render import Renderer
from .schedulers import JobState, Resources, pick
from .sync import GitignoreFilter
from .targets import find_root, probe_host, resolve, smallest_fit, ssh_hosts


def recorded(command: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: time the command and append it to the CLI's history (ok or error)."""

    @functools.wraps(command)
    def wrapper(self: Fleet, *args: Any, **kwargs: Any) -> Any:
        started = monotonic()
        try:
            result = command(self, *args, **kwargs)
        except BaseException as error:  # record, then let fire report it
            self._history.record(command.__name__, args, started, "error", detail=repr(error))
            raise
        handle = result if isinstance(result, str) else None
        self._history.record(command.__name__, args, started, "ok", handle=handle)
        return result

    return wrapper


def connect(name: str) -> SshMachine:
    """Open an ssh connection to ``name`` with ``~/.cargo/bin`` (pueue) on PATH."""
    remote = SshMachine(name)
    remote.env.path.insert(0, remote.cwd / ".cargo" / "bin")
    return remote


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


class Fleet:
    """The ``fleet`` CLI: onboard hosts, dispatch jobs, pull results back."""

    def __init__(self) -> None:
        # The on-host executor (`fleet exec ...`) is the only eager dependency:
        # it is cheap and must work on a bare remote with no fleet.toml or `.fleet/`.
        # Control-plane state below is lazy, so `fleet exec` never reads them.
        self.exec = Executor()

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
                history.path.with_name("fleet.log"),
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
    def discover(self, target: str) -> None:
        """Onboard ``target`` (probe + sync + ``pixi install``) and show what it is."""
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
        script: str,
        *args: str,
        needs: float | None = None,
        fetch: str | None = None,
    ) -> str:
        """rsync the repo to ``target`` (``auto`` routes by ``--needs``) and run ``script``."""
        if target == "auto":
            if needs is None:
                raise SystemExit("`--needs <GB>` is required when target is `auto`")
            machine = smallest_fit(self._known_targets(), float(needs))
        else:
            machine = self._target(target)
        self._rsync_up(machine)
        sha = git("rev-parse", "--short", "HEAD")
        dirty = bool(git("status", "--porcelain"))
        with connect(machine.name) as remote:
            handle = pick(machine).submit(
                remote, machine.root, script, args, resources=Resources()
            )
        self._cache.record(
            {
                "handle": handle,
                "target": machine.name,
                "kind": machine.kind,
                "script": script,
                "args": " ".join(shlex.quote(a) for a in args),
                "git_sha": sha,
                "dirty": int(dirty),
                "submitted_at": pendulum.now().to_iso8601_string(),
                "fetch_path": fetch,
            }
        )
        logger.info(
            "{} -> {} on {} ({}{})", script, handle, machine.name, sha, "+dirty" if dirty else ""
        )
        print(handle)
        return handle

    @recorded
    def ps(self, limit: int = 20) -> None:
        """Show the most recent dispatched runs across every target."""
        self._render.runs(self._cache.recent(limit))

    @recorded
    def status(self, target: str) -> None:
        """Show live jobs on ``target``."""
        machine = self._target(target)
        with connect(machine.name) as remote:
            pick(machine).status(remote, machine.root)

    @recorded
    def reconcile(self, target: str) -> None:
        """Compare the cache's recorded runs for ``target`` with the live scheduler.

        Shows each run's live state, exit code, and a verdict (ok / failed /
        running / vanished) — the local-state debugging aid that replaces email.
        """
        machine = self._target(target)
        runs = [r for r in self._cache.recent(limit=1000) if r["target"] == machine.name]
        scheduler = pick(machine)
        with connect(machine.name) as remote:
            rows = [
                row(
                    scheduler.state(remote, machine.root, r["handle"]),
                    script=r["script"],
                    submitted_at=r["submitted_at"],
                )
                for r in runs
            ]
        self._render.reconcile(rows)

    @recorded
    def interact(self, target: str, gpus: int = 1, hours: int = 2, dry_run: bool = False) -> None:
        """Grab an interactive session on ``target`` (a real TTY).

        A PBS target submits an interactive ``qsub -I`` with the discovered
        account (the user's group) and interactive queue; an ssh target opens a
        login shell.

        gpus: nodes/GPUs to request (``select=``).
        hours: requested walltime in hours.
        dry_run: print the command instead of running it.
        """
        machine = self._target(target)
        if machine.kind != "pbs":
            run_tty(["ssh", "-t", machine.name], dry_run)
            return
        flags = ["qsub", "-I", "-l", f"select={gpus}", "-l", f"walltime={max(hours, 1):02d}:00:00"]
        if machine.queue:
            flags += ["-q", machine.queue]
        if machine.account:
            flags += ["-W", f"group_list={machine.account}"]
        run_tty(["ssh", "-t", machine.name, f"bash -lc {shlex.quote(shlex.join(flags))}"], dry_run)

    @recorded
    def logs(self, target: str, handle: str, follow: bool = False) -> None:
        """Tail the run log for ``handle`` on ``target``."""
        machine = self._target(target)
        with connect(machine.name) as remote:
            pick(machine).logs(remote, machine.root, str(handle), follow=follow)

    @recorded
    def info(self, target: str, handle: str) -> None:
        """Show a job's post-mortem (PBS: exit status, mem used vs cap, GPU usage)."""
        machine = self._target(target)
        with connect(machine.name) as remote:
            state = pick(machine).state(remote, machine.root, str(handle))
        self._render.reconcile([row(state)])

    @recorded
    def fetch(self, target: str, path: str) -> None:
        """rsync ``path`` (relative to the repo root) back from ``target``."""
        self._fetch(target, path)

    @recorded
    def pull(self, handle: str) -> None:
        """rsync back the results path recorded for ``handle`` at submit time."""
        run = self._cache.run(handle)
        if not run["fetch_path"]:
            raise SystemExit(
                f"run {handle!r} has no fetch path; use `fleet fetch {run['target']} <path>`"
            )
        self._fetch(run["target"], run["fetch_path"])

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
        """Show the most recent ``fleet`` command invocations."""
        self._render.history(self._history.recent(limit))

    def _targets(self) -> list[str]:
        """Target aliases: ``fleet.toml`` overrides, else ``~/.ssh/config``."""
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
        """Find the root, rsync, ``pixi install``, then probe in-env for a Target.

        Cached only once ``pixi install`` succeeds: ``setup.sh`` runs under
        ``set -e``, so a failed install raises before ``save_facts`` — a machine
        that can't build the env never enters the fleet.
        """
        setup = (Path(__file__).parent / "scripts" / "setup.sh").read_text()
        with connect(alias) as remote:
            root = find_root(remote)
            self._rsync_up(Target(name=alias, root=root))
            remote["bash"][["-c", setup, "fleet-setup", root]] & FG
            facts = probe_host(remote, alias, root)
        self._cache.save_facts(alias, facts)
        machine = resolve(alias, self._config, facts)
        logger.info("onboarded {} ({}, {})", alias, machine.kind, machine.root)
        return machine

    def _rsync_up(self, machine: Target) -> None:
        """Ship the repo to ``machine``; git-ignored files and the fleet.toml denylist skipped."""
        rsync(
            self._config.sync.include,
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


def app() -> None:
    """Console-script entry point for ``fleet``."""
    fire.Fire(Fleet)


if __name__ == "__main__":
    app()
