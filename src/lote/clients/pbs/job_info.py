from pathlib import Path

from ...base import Field, Model
from .job_state import PbsState


class JobInfo(Model):
    """Parsed PBS job information."""

    job_id: str
    name: str
    user: str
    state: PbsState | str
    queue: str
    exit_status: int | None = None  # set only for a finished job (qstat -x history), else None
    server: str | None = None
    project: str | None = None
    group: str | None = None
    walltime: str | None = None
    walltime_used: str | None = None
    comment: str | None = None
    output_path: Path | None = None
    error_path: Path | None = None
    resources_requested: dict[str, str] = Field(default_factory=dict)
    resources_used: dict[str, str] = Field(default_factory=dict)
    variables: dict[str, str] = Field(default_factory=dict)
