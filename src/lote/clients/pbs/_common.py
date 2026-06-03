from __future__ import annotations

import re

from .job_state import JobState


def extract_job_id(output: str) -> str:
    """Extract the PBS job identifier from `qsub` output."""

    if match := re.match(r"^(\d+(?:\[[^\]]*\])?)\.?.*$", output.strip()):
        return match.group(1)
    return output.strip()


_WORD_STATE_ALIASES: dict[str, JobState] = {
    "RUNNING": JobState.RUNNING,
    "QUEUED": JobState.QUEUED,
    "WAITING": JobState.WAITING,
    "HELD": JobState.HELD,
    "EXITING": JobState.EXITING,
    "FINISHED": JobState.FINISHED,
    "MOVED": JobState.MOVED,
    "SUSPENDED": JobState.SUSPENDED,
    "BEGUN": JobState.ARRAY_BEGUN,
}


def parse_job_state(value: str) -> JobState | str:
    """Parse a PBS job-state token (single letter or full word)."""

    try:
        return JobState(value)
    except ValueError:
        return _WORD_STATE_ALIASES.get(value.upper(), value)


def parse_variable_list(value: str) -> dict[str, str]:
    """Parse a PBS `Variable_List` value."""

    variables: dict[str, str] = {}
    for entry in value.split(","):
        if "=" not in entry:
            continue
        key, raw_value = entry.split("=", maxsplit=1)
        variables[key] = raw_value
    return variables
