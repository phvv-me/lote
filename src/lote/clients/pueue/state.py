from enum import StrEnum


class PueueState(StrEnum):
    """pueue task lifecycle — the externally-tagged key of a task's ``status``."""

    LOCKED = "Locked"
    STASHED = "Stashed"
    QUEUED = "Queued"
    RUNNING = "Running"
    PAUSED = "Paused"
    DONE = "Done"
