from ..base import FrozenModel
from .node import NodeClass


class MemoryReading(FrozenModel):
    """A memory pool's capacity, as mainboard reports it.

    total_bytes: total capacity of the pool.
    """

    total_bytes: int = 0


class CpuReading(FrozenModel):
    """The probed node's CPU identity and core count.

    name: CPU model name.
    logical_cores: logical CPU threads.
    """

    name: str = ""
    logical_cores: int = 0


class GpuReading(FrozenModel):
    """One probed GPU's identity and memory.

    unit_name: full device name (e.g. ``NVIDIA H100``).
    memory: the device's memory pool.
    """

    unit_name: str = ""
    memory: MemoryReading = MemoryReading()


class Snapshot(FrozenModel):
    """The slice of mainboard's ``MachineSnapshot`` JSON the queue probe reads.

    The probe job prints ``Machine().model_dump_json()`` on a node of the
    class; pydantic ignores every telemetry field this slice does not name, so
    mainboard can grow without breaking lote.

    hostname: network name of the probed node.
    cpu: the node's CPU identity and capacity.
    memory: the node's system RAM.
    gpus: the node's GPUs, empty when none are present.
    """

    hostname: str = ""
    cpu: CpuReading = CpuReading()
    memory: MemoryReading = MemoryReading()
    gpus: tuple[GpuReading, ...] = ()

    def node_class(self, name: str) -> NodeClass:
        """Project this snapshot onto the :class:`NodeClass` cached under ``name``.

        name: the class key the capabilities are cached under (the queue name).
        """
        gpu = self.gpus[0] if self.gpus else None
        return NodeClass(
            name=name,
            gpu_name=(gpu.unit_name or None) if gpu else None,
            gpu_count=len(self.gpus),
            gpu_mem_mb=(round(gpu.memory.total_bytes / 1024**2) or None) if gpu else None,
            sysmem_gb=round(self.memory.total_bytes / 1024**3) or None,
            cpu_cores=self.cpu.logical_cores or None,
            hostname=self.hostname or None,
        )
