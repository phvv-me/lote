from ...base import FrozenModel


class ResourceSpec(FrozenModel):
    """Structured PBS resource list.

    select: number of chunks or nodes.
    ncpus: CPUs per chunk.
    mpiprocs: MPI ranks per chunk.
    ompthreads: OpenMP threads per chunk.
    mem: memory request such as `32gb`.
    walltime: requested walltime in `HH:MM:SS`.
    place: PBS placement mode.
    host: requested host.
    vnode: requested vnode.
    software: software selector.
    """

    select: int | str | None = None
    ncpus: int | None = None
    mpiprocs: int | None = None
    ompthreads: int | None = None
    mem: str | None = None
    walltime: str | None = None
    place: str | None = None
    host: str | None = None
    vnode: str | None = None
    software: str | None = None

    def to_select_clause(self) -> str:
        """Render the chunked `select=` clause."""

        parts: list[str] = []
        if self.select is not None:
            parts.append(f"select={self.select}")
        if self.ncpus is not None:
            parts.append(f"ncpus={self.ncpus}")
        if self.mpiprocs is not None:
            parts.append(f"mpiprocs={self.mpiprocs}")
        if self.ompthreads is not None:
            parts.append(f"ompthreads={self.ompthreads}")
        if self.mem is not None:
            parts.append(f"mem={self.mem}")
        return ":".join(parts)

    def extra_clauses(self) -> list[str]:
        """Render non-`select` resource clauses."""

        values = {
            "walltime": self.walltime,
            "place": self.place,
            "host": self.host,
            "vnode": self.vnode,
            "software": self.software,
        }
        return [f"{key}={value}" for key, value in values.items() if value is not None]
