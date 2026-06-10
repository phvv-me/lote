import shlex
from pathlib import Path

from plumbum import local

from ...log import logger
from ..machine import Machine
from ._common import parse_job_state, parse_variable_list
from .job_info import JobInfo
from .job_state import PbsState


def qstat(
    job_ids: str | list[str] | None = None,
    *,
    all_jobs: bool = False,
    full_output: bool = False,
    show_arrays: bool = False,
    history: bool = False,
    queue: str | None = None,
    user: str | None = None,
    machine: Machine = local,
    parse_output: bool = True,
    dry_run: bool = False,
    retcode: int | None = 0,
) -> list[JobInfo] | str:
    """Run `qstat` on ``machine`` and optionally parse the output.

    retcode: the exit code plumbum enforces. Pass ``None`` to tolerate a
        non-zero exit (PBS exits non-zero for finished or unknown job ids).
    """

    command = ["qstat"]
    if all_jobs:
        command.append("-a")
    elif full_output:
        command.append("-f")
    if show_arrays:
        command.append("-t")
    if history:
        command.append("-H")  # include finished jobs (Miyabi's qstat history flag)
    if queue is not None:
        command.extend(["-Q", queue])
    if user is not None:
        command.extend(["-u", user])
    if job_ids is not None:
        command.extend([job_ids] if isinstance(job_ids, str) else job_ids)
    if dry_run:
        return shlex.join(command)
    logger.info("running {}", shlex.join(command))
    output = machine[command[0]][command[1:]](retcode=retcode)
    if not parse_output:
        return str(output)
    return parse_qstat_full(output) if full_output else parse_qstat_output(output)


def parse_qstat_output(output: str) -> list[JobInfo]:
    """Parse `qstat` / `qstat -a` output across PBS variants.

    Supports both the standard PBS layout (``Job ID  Username Queue ...``)
    and a wide vendor variant (``JOB_ID JOB_NAME STATUS PROJECT QUEUE
    START_DATE ELAPSE TOKEN NODE MIG``). The latter has a two-token
    START_DATE column, so naive whitespace splitting needs care.
    """
    jobs: list[JobInfo] = []
    lines = output.splitlines()
    header_index = -1
    wide_format = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("Job ID", "Job id", "Job")):
            header_index = index
            wide_format = False
            break
        if stripped.startswith("JOB_ID"):
            header_index = index
            wide_format = True
            break
    if header_index == -1:
        return jobs
    body_start = header_index + 1
    if body_start < len(lines) and lines[body_start].lstrip().startswith("---"):
        body_start += 1
    for line in lines[body_start:]:
        parts = line.split()
        if not wide_format:
            if len(parts) < 5:
                continue
            if len(parts) >= 6:
                job_id, name, user, walltime_used, state, queue = parts[:6]
            else:
                job_id, name, user, state, queue = parts[:5]
                walltime_used = None
            jobs.append(
                JobInfo(
                    job_id=job_id,
                    name=name,
                    user=user,
                    state=parse_job_state(state),
                    queue=queue,
                    walltime_used=walltime_used,
                ),
            )
            continue
        if len(parts) < 8:
            continue
        job_id, name, status, project, queue = parts[:5]
        start_date = f"{parts[5]} {parts[6]}" if len(parts) >= 11 else parts[5]
        walltime_used_index = 7 if len(parts) >= 11 else 6
        walltime_used = parts[walltime_used_index]
        jobs.append(
            JobInfo(
                job_id=job_id,
                name=name,
                user="",
                state=parse_job_state(status),
                queue=queue,
                project=project,
                walltime_used=walltime_used if walltime_used != "--:--:--" else None,
                resources_used={"start_date": start_date} if start_date else {},
            ),
        )
    return jobs


def parse_qstat_full(output: str) -> list[JobInfo]:
    """Parse `qstat -f` output."""

    jobs: list[JobInfo] = []
    current: JobInfo | None = None
    for line in output.splitlines():
        if line.startswith("Job Id:"):
            if current is not None:
                jobs.append(current)
            current = JobInfo(
                job_id=line.split(":", maxsplit=1)[1].strip(),
                name="",
                user="",
                state=PbsState.QUEUED,
                queue="",
            )
            continue
        if current is None or " = " not in line:
            continue
        key, value = line.strip().split(" = ", maxsplit=1)
        match key:
            case "Job_Name":
                current.name = value
            case "Job_Owner":
                current.user = value.split("@", maxsplit=1)[0]
            case "job_state":
                current.state = parse_job_state(value)
            case "queue":
                current.queue = value
            case "server":
                current.server = value
            case "project":
                current.project = value
            case "egroup":
                current.group = value
            case "Resource_List.walltime":
                current.walltime = value
                current.resources_requested["walltime"] = value
            case "resources_used.walltime":
                current.walltime_used = value
                current.resources_used["walltime"] = value
            case "Output_Path":
                current.output_path = Path(value)
            case "Error_Path":
                current.error_path = Path(value)
            case "comment":
                current.comment = value
            case "Variable_List":
                current.variables.update(parse_variable_list(value))
            case _ if key.startswith("Resource_List."):
                current.resources_requested[key.removeprefix("Resource_List.")] = value
            case _ if key.startswith("resources_used."):
                current.resources_used[key.removeprefix("resources_used.")] = value
    if current is not None:
        jobs.append(current)
    return jobs
