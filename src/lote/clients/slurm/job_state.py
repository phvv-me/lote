from enum import StrEnum


class SlurmState(StrEnum):
    """SLURM job states reported by ``squeue``/``sacct`` (the long form names).

    These are the ``State`` strings ``sacct`` emits; ``squeue`` uses the same
    long names under its ``%T`` field. ``sacct`` also appends a node reason in
    parentheses for ``CANCELLED by <uid>`` which is stripped before lookup.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    COMPLETING = "COMPLETING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    NODE_FAIL = "NODE_FAIL"
    OUT_OF_MEMORY = "OUT_OF_MEMORY"
    BOOT_FAIL = "BOOT_FAIL"
    DEADLINE = "DEADLINE"
    PREEMPTED = "PREEMPTED"


# States in which a job is still in flight (not a terminal verdict).
SLURM_LIVE = {
    SlurmState.PENDING,
    SlurmState.RUNNING,
    SlurmState.SUSPENDED,
    SlurmState.COMPLETING,
}
