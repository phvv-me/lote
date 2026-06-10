import shlex
from pathlib import Path

from plumbum import local

from ...log import logger
from ..machine import Machine
from ._common import extract_job_id
from .job_dependency import JobDependency
from .resource_spec import ResourceSpec


def build_qsub_command(
    *,
    script: Path | str | None = None,
    queue: str,
    group_list: str,
    select: int | str,
    walltime: str | None = None,
    ncpus: int | None = None,
    mpiprocs: int | None = None,
    ompthreads: int | None = None,
    mem: str | None = None,
    place: str | None = None,
    job_name: str | None = None,
    stdout_path: Path | str | None = None,
    stderr_path: Path | str | None = None,
    join_output: bool = False,
    dependency: JobDependency | str | None = None,
    variable_list: dict[str, str] | str | None = None,
    export_all_vars: bool = False,
    rerunnable: bool = True,
    interactive: bool = False,
    resource_list: ResourceSpec | None = None,
    extra_resources: dict[str, str] | None = None,
) -> list[str]:
    """Build a `qsub` command."""

    resource_spec = ResourceSpec(
        select=select,
        ncpus=ncpus,
        mpiprocs=mpiprocs,
        ompthreads=ompthreads,
        mem=mem,
        walltime=walltime,
        place=place,
    )
    if resource_list is not None:
        resource_spec = ResourceSpec(
            select=resource_list.select
            if resource_list.select is not None
            else resource_spec.select,
            ncpus=resource_list.ncpus if resource_list.ncpus is not None else resource_spec.ncpus,
            mpiprocs=resource_list.mpiprocs
            if resource_list.mpiprocs is not None
            else resource_spec.mpiprocs,
            ompthreads=resource_list.ompthreads
            if resource_list.ompthreads is not None
            else resource_spec.ompthreads,
            mem=resource_list.mem if resource_list.mem is not None else resource_spec.mem,
            walltime=resource_list.walltime
            if resource_list.walltime is not None
            else resource_spec.walltime,
            place=resource_list.place if resource_list.place is not None else resource_spec.place,
            host=resource_list.host,
            vnode=resource_list.vnode,
            software=resource_list.software,
        )

    command = ["qsub", "-q", queue, "-W", f"group_list={group_list}"]
    if select_clause := resource_spec.to_select_clause():
        command.extend(["-l", select_clause])
    for clause in resource_spec.extra_clauses():
        command.extend(["-l", clause])
    for key, value in (extra_resources or {}).items():
        command.extend(["-l", f"{key}={value}"])
    if job_name is not None:
        command.extend(["-N", job_name])
    if stdout_path is not None:
        command.extend(["-o", str(stdout_path)])
    if stderr_path is not None:
        command.extend(["-e", str(stderr_path)])
    if join_output:
        command.extend(["-j", "oe"])  # merge stderr into stdout: one chronological log
    if dependency is not None:
        dependency_value = (
            dependency.to_pbs_string() if isinstance(dependency, JobDependency) else dependency
        )
        command.extend(["-W", f"depend={dependency_value}"])
    if variable_list is not None:
        values = (
            ",".join(f"{key}={value}" for key, value in variable_list.items())
            if isinstance(variable_list, dict)
            else variable_list
        )
        command.extend(["-v", values])
    if export_all_vars:
        command.extend(["-V"])
    command.extend(["-r", "y" if rerunnable else "n"])
    if interactive:
        command.append("-I")
    if script is not None:
        command.append(str(script))
    return command


def qsub(
    *,
    script: Path | str | None = None,
    queue: str,
    group_list: str,
    select: int | str,
    walltime: str | None = None,
    ncpus: int | None = None,
    mpiprocs: int | None = None,
    ompthreads: int | None = None,
    mem: str | None = None,
    place: str | None = None,
    job_name: str | None = None,
    stdout_path: Path | str | None = None,
    stderr_path: Path | str | None = None,
    join_output: bool = False,
    dependency: JobDependency | str | None = None,
    variable_list: dict[str, str] | str | None = None,
    export_all_vars: bool = False,
    rerunnable: bool = True,
    interactive: bool = False,
    resource_list: ResourceSpec | None = None,
    extra_resources: dict[str, str] | None = None,
    machine: Machine = local,
    stdin: str | None = None,
    dry_run: bool = False,
) -> str:
    """Submit a PBS job on ``machine`` (``local`` or ``SshMachine``), or render it in dry-run."""

    command = build_qsub_command(
        script=script,
        queue=queue,
        group_list=group_list,
        select=select,
        walltime=walltime,
        ncpus=ncpus,
        mpiprocs=mpiprocs,
        ompthreads=ompthreads,
        mem=mem,
        place=place,
        job_name=job_name,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        join_output=join_output,
        dependency=dependency,
        variable_list=variable_list,
        export_all_vars=export_all_vars,
        rerunnable=rerunnable,
        interactive=interactive,
        resource_list=resource_list,
        extra_resources=extra_resources,
    )
    if dry_run:
        return shlex.join(command)
    logger.info("running {}", shlex.join(command))
    cmd = machine[command[0]][command[1:]]
    return extract_job_id((cmd << stdin)() if stdin is not None else cmd())
