"""The ``lote exec`` on-host executor: find a job script, submit it
(``qsub``/``sbatch``) or run it (``bash``), and monitor it.

Use it directly on a login node; ``lote`` also calls it on remote hosts via
``chefe run lote exec ...``. One job script, three environments, no code changes:

- **PBS cluster**: ``lote exec qsub <script>`` reads ``#PBS`` directives and
  submits via :mod:`lote.clients.pbs`, returning the job id.
- **SLURM cluster**: ``lote exec sbatch <script>`` reads ``#SBATCH`` directives
  and submits via :mod:`lote.clients.slurm`.
- **Plain host** (DGX, PC, interactive node): ``lote exec run <script>`` runs the
  script through ``bash`` directly, no scheduler. Job scripts guard
  ``module load`` with ``command -v module`` so they no-op off a cluster.

Subcommands::

    lote exec qsub    <script.sh> [-- args ...]   # submit to PBS, returns JID
    lote exec sbatch  <script.sh> [-- args ...]   # submit to SLURM, returns JID
    lote exec run     <script.sh> [-- args ...]   # bash run, no scheduler
    lote exec status                              # rich table of my jobs
    lote exec info    <jid>                        # post-mortem record
    lote exec logs    <jid|name> [--follow]        # tail latest .log
    lote exec cancel  <jid|name|all>               # qdel / scancel

``status``/``logs`` rely on the convention that each job writes to
``projects/<pkg>/experiments/<exp>/logs/<name>/<jobid>.log`` and uses its
job-name as the label, so a scheduler row maps to its log.
"""

from __future__ import annotations

import grp
import os
import re
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import fire
from rich.console import Console
from rich.table import Table

from ..clients.pbs import (
    JobInfo,
    JobState,
    qdel,
    qstat,
    qsub,
)
from ..clients.slurm import (
    SlurmJob,
    SlurmState,
    sacct,
    sbatch,
    scancel,
    squeue,
)
from ..log import logger

PBS_DIRECTIVE_RE = re.compile(r"^\s*#PBS\s+(.*)$")
SBATCH_DIRECTIVE_RE = re.compile(r"^\s*#SBATCH\s+(.*)$")


def experiments_root() -> Path:
    """Directory holding ``projects/<pkg>/experiments`` (the repo's ``research/`` tree).

    Found by walking up from the CWD to the monorepo root (the ``pixi.toml`` dir),
    so it resolves regardless of where a ``pixi run`` task starts.
    """
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "pixi.toml").exists():
            return candidate / "research"
    return Path.cwd()


def _parse_pbs_directives(script: Path) -> dict[str, str]:
    """Extract ``#PBS -<flag> <value>`` directives from a job script.

    Flags are stored verbatim under their option letter. ``-l`` directives
    are concatenated with newlines so ``to_args`` can re-emit each line.
    """
    text = script.read_text()
    directives: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not (match := PBS_DIRECTIVE_RE.match(line)):
            continue
        body = match.group(1).strip()
        if not body.startswith("-"):
            continue
        flag, _, value = body[1:].partition(" ")
        directives.setdefault(flag, []).append(value.strip())
    return {flag: "\n".join(values) for flag, values in directives.items()}


def _parse_sbatch_directives(script: Path) -> dict[str, str]:
    """Extract ``#SBATCH`` directives from a job script as ``{long-name: value}``.

    Both ``--time=02:00:00`` and the short ``-t 02:00:00`` forms are normalised
    to their long option name (``time``), so callers read a single key. The last
    occurrence of a flag wins, mirroring SLURM's own precedence.
    """
    short_to_long = {"t": "time", "p": "partition", "A": "account", "J": "job-name", "o": "output"}
    directives: dict[str, str] = {}
    for line in script.read_text().splitlines():
        if not (match := SBATCH_DIRECTIVE_RE.match(line)):
            continue
        body = match.group(1).strip()
        if body.startswith("--"):
            name, _, value = body[2:].partition("=")
            directives[name.strip()] = value.strip()
        elif body.startswith("-"):
            flag, _, value = body[1:].partition(" ")
            directives[short_to_long.get(flag, flag)] = value.strip()
    return directives


def _int_or_none(value: str | None) -> int | None:
    """Leading-integer of ``value`` (so ``32G`` -> 32, ``gpu:2`` -> None), else None."""
    if value is None:
        return None
    digits = re.match(r"\d+", value.strip())
    return int(digits.group(0)) if digits else None


def _has_command(name: str) -> bool:
    """Whether ``name`` is on PATH (the on-host scheduler probe, ``command -v``)."""
    return (
        subprocess.run(
            ["bash", "-lc", f"command -v {shlex.quote(name)}"], capture_output=True
        ).returncode
        == 0
    )


def _resolve_script(script: str) -> Path:
    """Resolve ``script`` to a real path, searching ``experiments/*/jobs`` if bare."""
    candidate = Path(script).expanduser()
    if candidate.is_file():
        return candidate
    matches = list(experiments_root().glob(f"projects/*/experiments/*/jobs/{script}*.sh"))
    if not matches:
        matches = list(Path.cwd().glob(f"experiments/*/jobs/{script}*.sh"))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise FileNotFoundError(
            f"ambiguous script {script!r}: {', '.join(str(m) for m in matches)}",
        )
    raise FileNotFoundError(f"no .sh found for {script!r}")


def _print_jobs_table(jobs: Sequence[JobInfo], *, console: Console) -> None:
    """Render a rich table summarising the given jobs."""
    table = Table(show_header=True, header_style="bold cyan")
    for column in ("JID", "Name", "State", "Queue", "Walltime", "Used"):
        table.add_column(column, justify="right" if column in ("Walltime", "Used") else "left")
    for job in jobs:
        state = job.state.value if isinstance(job.state, JobState) else str(job.state)
        state_style = {
            "R": "[green]R[/green]",
            "Q": "[yellow]Q[/yellow]",
            "H": "[red]H[/red]",
            "F": "[blue]F[/blue]",
            "E": "[magenta]E[/magenta]",
        }.get(state, state)
        table.add_row(
            job.job_id.split(".")[0],
            job.name,
            state_style,
            job.queue or "-",
            job.walltime or "-",
            job.walltime_used or "-",
        )
    console.print(table)


def _print_slurm_table(jobs: Sequence[SlurmJob], *, console: Console) -> None:
    """Render a rich table summarising SLURM jobs (mirrors the PBS table)."""
    table = Table(show_header=True, header_style="bold cyan")
    for column in ("JID", "Name", "State", "Partition", "Elapsed"):
        table.add_column(column, justify="right" if column == "Elapsed" else "left")
    palette = {
        SlurmState.RUNNING: "green",
        SlurmState.PENDING: "yellow",
        SlurmState.COMPLETED: "blue",
        SlurmState.FAILED: "red",
        SlurmState.CANCELLED: "magenta",
    }
    for job in jobs:
        color = palette.get(job.state, "white") if isinstance(job.state, SlurmState) else "white"
        state = str(job.state)
        table.add_row(
            job.job_id,
            job.name,
            f"[{color}]{state}[/{color}]",
            job.partition or "-",
            job.elapsed or "-",
        )
    console.print(table)


def _resolve_jid_or_name(target: str, jobs: Sequence[JobInfo]) -> list[str]:
    """Return matching job ids for ``target`` (full id, prefix, or name).

    The match is permissive on partial names because some PBS deployments
    truncate ``JOB_NAME`` to 10 characters in ``qstat`` output, so ``sampler_ab``
    is the only string available for what was submitted as
    ``sampler_ablation``.
    """
    matches: list[str] = []
    for job in jobs:
        jid = job.job_id.split(".")[0]
        if target in (job.job_id, jid, job.name):
            matches.append(job.job_id)
            continue
        if job.name and (job.name.startswith(target) or target.startswith(job.name)):
            matches.append(job.job_id)
    return matches


class Executor:
    """The ``lote exec`` on-host executor: submit/run/monitor one job here."""

    def qsub(
        self,
        script: str,
        *args: str,
        queue: str | None = None,
        walltime: str | None = None,
        select: int | None = None,
        group_list: str | None = None,
        dry_run: bool = False,
    ) -> str:
        """Submit ``script`` to PBS; return the job id.

        script: path to a ``.sh`` (absolute or relative) or a bare name
            resolved against ``experiments/*/jobs/<name>*.sh``.
        args: extra positional arguments appended to the job's command
            via ``ARGS=...`` env var; the script reads ``$ARGS`` and
            forwards them to its python entry point.
        queue / walltime / select: override the script's ``#PBS``
            directives.
        group_list: PBS account string; defaults to the user's primary group.
        dry_run: print the rendered ``qsub`` command without running it.
        """
        path = _resolve_script(script)
        directives = _parse_pbs_directives(path)
        effective_queue = queue or directives.get("q") or "gen-S"
        effective_walltime = walltime
        effective_select: int | str | None = select
        for line in directives.get("l", "").splitlines():
            if "walltime=" in line and effective_walltime is None:
                effective_walltime = line.split("walltime=", maxsplit=1)[1].strip()
            if line.startswith("select=") and effective_select is None:
                effective_select = line.removeprefix("select=")
        if effective_select is None:
            effective_select = 1
        job_name = directives.get("N", path.stem)
        logs_dir = path.parent.parent / "logs" / job_name  # experiments/<exp>/logs/<name>/
        logs_dir.mkdir(parents=True, exist_ok=True)
        env_args = " ".join(shlex.quote(a) for a in args)
        return qsub(
            script=path,
            queue=effective_queue,
            group_list=group_list or grp.getgrgid(os.getgid()).gr_name,
            select=effective_select,
            walltime=effective_walltime,
            job_name=job_name,
            stdout_path=f"{logs_dir}/",  # PBS writes <name>.o<jid> here
            join_output=True,  # merge stderr so crashes / OOM kills are captured too
            variable_list={"ARGS": env_args} if env_args else None,
            export_all_vars=False,  # the job sets up its own env via `module load`; -V would
            dry_run=dry_run,  # also export multi-byte activation vars that PBS rejects
        )

    def sbatch(
        self,
        script: str,
        *args: str,
        partition: str | None = None,
        walltime: str | None = None,
        gpus: int | None = None,
        account: str | None = None,
        mem_gb: int | None = None,
        dry_run: bool = False,
    ) -> str:
        """Submit ``script`` to SLURM; return the job id.

        Mirrors :meth:`qsub`: parse the script's ``#SBATCH`` directives, point the
        merged stdout+stderr sink at ``experiments/<exp>/logs/<name>/``, forward
        positional ``args`` through the ``ARGS`` env var, and submit.

        partition / walltime / gpus / account / mem_gb: override the script's
            ``#SBATCH`` directives.
        dry_run: print the rendered ``sbatch`` command without running it.
        """
        path = _resolve_script(script)
        directives = _parse_sbatch_directives(path)
        job_name = directives.get("job-name", path.stem)
        logs_dir = path.parent.parent / "logs" / job_name  # experiments/<exp>/logs/<name>/
        logs_dir.mkdir(parents=True, exist_ok=True)
        env_args = " ".join(shlex.quote(a) for a in args)
        gpus_value = gpus if gpus is not None else _int_or_none(directives.get("gpus"))
        return sbatch(
            script=path,
            gpus=gpus_value if gpus_value is not None else 1,
            walltime=walltime or directives.get("time"),
            partition=partition or directives.get("partition"),
            account=account or directives.get("account"),
            mem_gb=mem_gb if mem_gb is not None else _int_or_none(directives.get("mem")),
            job_name=job_name,
            output_path=f"{logs_dir}/%j.log",  # %j -> job id; SLURM merges stderr by default
            export_vars={"ARGS": env_args} if env_args else None,
            dry_run=dry_run,
        )

    def run(self, script: str, *args: str) -> int:
        """Run ``script`` directly through ``bash`` -- no scheduler involved."""
        path = _resolve_script(script)
        env = {**os.environ, "ARGS": " ".join(shlex.quote(a) for a in args)}
        logger.info("running locally: bash {} {}", path, env["ARGS"])
        result = subprocess.run(
            ["bash", str(path)],
            env=env,
            check=False,
        )
        return int(result.returncode)

    def status(self, all_users: bool = False) -> None:
        """Print a Rich table of my live jobs, picking the host's scheduler.

        On a SLURM host this is ``squeue --me``; otherwise ``qstat`` (whose bare
        form already returns only the current user's jobs uncensored).
        ``all_users=True`` widens the PBS view to ``qstat -a``.
        """
        console = Console()
        if _has_command("squeue"):
            slurm_jobs = squeue(me=not all_users)
            if not isinstance(slurm_jobs, list) or not slurm_jobs:
                console.print("[yellow]no jobs[/yellow]")
                return
            _print_slurm_table(slurm_jobs, console=console)
            return
        jobs = qstat(all_jobs=all_users)
        if not jobs:
            console.print("[yellow]no jobs[/yellow]")
            return
        if not isinstance(jobs, list):
            print(jobs)
            return
        _print_jobs_table(jobs, console=console)

    def info(self, jid: str, history: bool = True) -> None:
        """Print one job's post-mortem record, picking the host's scheduler.

        SLURM: ``sacct`` State + ExitCode. PBS: the full ``qstat -f`` record
        (history-aware, so finished jobs still resolve) showing ``Exit_status``,
        ``resources_used.mem`` vs the default cap, GPU usage and walltime.
        """
        if _has_command("sacct"):
            print(sacct(str(jid), parse_output=False))
            return
        out = qstat(job_ids=str(jid), full_output=True, history=history, parse_output=False)
        print(out)

    def logs(self, target: str, follow: bool = False, lines: int = 200) -> None:
        """Tail a job's captured output (merged stdout+stderr) by job id or name.

        Globs the experiment ``logs/`` dirs, so a crashed job's output is
        retrievable even after the job has dropped out of ``qstat``.
        """
        target = str(target)  # fire may parse a numeric job id as int
        if Path(target).exists():
            log = Path(target)
        else:
            matches = [
                p
                for p in experiments_root().glob(f"projects/*/experiments/*/logs/**/*{target}*")
                if p.is_file()
            ]
            if not matches:
                matches = [
                    p for p in Path.cwd().glob(f"experiments/*/logs/**/*{target}*") if p.is_file()
                ]
            if not matches:
                raise FileNotFoundError(
                    f"no log for {target!r} (the job may not have started or flushed output yet)"
                )
            log = max(matches, key=lambda p: p.stat().st_mtime)
        cmd = ["tail", f"-n{lines}", *(["-f"] if follow else []), str(log)]
        subprocess.run(cmd, check=False)

    def cancel(self, target: str | int, force: bool = False) -> None:
        """Cancel a job by id, name, or ``all`` for every job of mine.

        On a SLURM host this resolves through ``squeue`` + ``scancel``; otherwise
        ``qstat`` + ``qdel``.
        """
        target_str = str(target)
        if _has_command("scancel"):
            self.__cancel_slurm(target_str)
            return
        jobs = qstat()
        if not isinstance(jobs, list):
            raise RuntimeError("qstat parsing failed")
        if target_str == "all":
            ids = [j.job_id for j in jobs]
        else:
            ids = _resolve_jid_or_name(target_str, jobs)
        if not ids:
            Console().print(f"[yellow]no match for {target!r}[/yellow]")
            return
        for jid in ids:
            qdel(jid, force=force)
            logger.info("cancelled {}", jid)

    def __cancel_slurm(self, target: str) -> None:
        """Cancel SLURM jobs matching ``target`` (id, name, or ``all``)."""
        jobs = squeue(me=True)
        if not isinstance(jobs, list):
            raise RuntimeError("squeue parsing failed")
        if target == "all":
            ids = [job.job_id for job in jobs]
        else:
            ids = [
                job.job_id
                for job in jobs
                if target in (job.job_id, job.name) or job.name.startswith(target)
            ]
        if not ids:
            Console().print(f"[yellow]no match for {target!r}[/yellow]")
            return
        for jid in ids:
            scancel(jid)
            logger.info("cancelled {}", jid)


def main() -> None:
    """Entry point for ``python -m lote.executor.cli``."""
    fire.Fire(Executor)


if __name__ == "__main__":
    main()
