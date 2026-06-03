from __future__ import annotations

from pydantic import ConfigDict

from ..base import FrozenModel


class Target(FrozenModel):
    """A resolved target: ssh alias + discovered capabilities + hints.

    The in-env probe builds one of these and prints ``model_dump_json``; the
    laptop reads it back with ``model_validate`` (see ``targets.resolve``).

    name: the ``~/.ssh/config`` Host alias (also the ssh destination).
    kind: ``ssh`` (no scheduler, pueue), ``pbs`` (qsub), or ``slurm`` (sbatch).
    root: remote monorepo path.
    gpu_name: full GPU name as reported by ``nvidia-smi`` (e.g. ``NVIDIA GB10``).
    gpu_mem_mb: GPU memory in MiB, when a GPU is present.
    sysmem_gb: system memory in GiB (the fallback when there is no GPU).
    account: charging account / PBS ``group_list``, discovered as the user's group.
    queue: the host's interactive queue, when one was discovered.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    kind: str = "ssh"
    root: str = "~/projects"
    gpu_name: str | None = None
    gpu_mem_mb: int | None = None
    sysmem_gb: int | None = None
    account: str | None = None
    queue: str | None = None

    @property
    def arch(self) -> str | None:
        """Short GPU arch label: ``gpu_name`` minus the ``NVIDIA`` prefix."""
        return (self.gpu_name or "").replace("NVIDIA", "").strip() or None

    @property
    def vram_gb(self) -> float | None:
        """Usable memory in GB: GPU VRAM if present, else system memory."""
        if self.gpu_mem_mb is not None:
            return self.gpu_mem_mb / 1024
        if self.sysmem_gb is not None:
            return float(self.sysmem_gb)
        return None

    def fits(self, needs_gb: float) -> bool:
        """Whether this target's memory satisfies ``needs_gb``.

        needs_gb: requested memory in GB.
        """
        return self.vram_gb is not None and self.vram_gb >= needs_gb
