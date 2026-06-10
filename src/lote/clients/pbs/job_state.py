from enum import StrEnum


class PbsState(StrEnum):
    """PBS job states."""

    ARRAY_BEGUN = "B"
    EXITING = "E"
    FINISHED = "F"
    HELD = "H"
    MOVED = "M"
    QUEUED = "Q"
    RUNNING = "R"
    SUSPENDED = "S"
    WAITING = "W"
