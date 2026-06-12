from ..base import FrozenModel

# The class key of the ssh login node; every other class is a scheduler queue.
LOGIN = "login"


class NodeClass(FrozenModel):
    """One node class's probed capabilities on a host.

    A host is rarely one machine. The ssh login node is one class of node, and
    each scheduler queue (a PBS queue, a SLURM partition) fronts its own
    hardware. GPU queues, CPU queues, and special classes like Miyabi's
    ``prepost`` data movers all differ, so capabilities live per class, keyed
    by ``name``, instead of pretending the login node speaks for the cluster.

    name: the class key, :data:`LOGIN` for the ssh node, else the queue name.
    gpu_name: full GPU name (e.g. ``NVIDIA H100``), when this class has one.
    gpu_count: GPUs per node in this class.
    gpu_mem_mb: one GPU's memory in MiB, when reported.
    sysmem_gb: system memory in GiB.
    cpu_cores: logical CPU cores per node.
    hostname: the node that answered the probe (one sample of the class).
    """

    name: str
    gpu_name: str | None = None
    gpu_count: int = 0
    gpu_mem_mb: int | None = None
    sysmem_gb: int | None = None
    cpu_cores: int | None = None
    hostname: str | None = None

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
