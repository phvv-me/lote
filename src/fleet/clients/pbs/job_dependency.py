from __future__ import annotations

from ...base import Field, FrozenModel
from .dependency_type import DependencyType


class JobDependency(FrozenModel):
    """Structured PBS dependency expression.

    kind: dependency kind.
    job_ids: dependent job IDs.
    """

    kind: DependencyType
    job_ids: list[str] = Field(default_factory=list)

    def to_pbs_string(self) -> str:
        """Render the dependency for `qsub -W depend=...`."""

        if not self.job_ids:
            return self.kind.value
        return f"{self.kind.value}:{':'.join(self.job_ids)}"
