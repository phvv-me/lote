import re

from .job_state import SlurmState


def extract_job_id(output: str) -> str:
    """Pull the job id out of ``sbatch`` output.

    ``sbatch`` prints ``Submitted batch job 12345`` on success; if a bare
    number is already given (some ``--parsable`` setups), return it as-is.
    """
    if match := re.search(r"Submitted batch job\s+(\d+)", output):
        return match.group(1)
    return output.strip().splitlines()[-1].strip() if output.strip() else ""


def parse_slurm_state(value: str) -> SlurmState | str:
    """Parse a SLURM state token, dropping any ``CANCELLED by 1000`` suffix."""
    head = value.strip().split(" ", maxsplit=1)[0].upper()
    try:
        return SlurmState(head)
    except ValueError:
        return value.strip()


def parse_exit_code(value: str) -> int | None:
    """Parse ``sacct``'s ``ExitCode`` field (``<returncode>:<signal>``).

    Returns the return code, or the signal number when the job was killed by a
    signal (return code 0 but signal non-zero). None when unparseable/empty.
    """
    field = value.strip()
    if not field:
        return None
    code, _, signal = field.partition(":")
    if code.isdigit() and int(code) != 0:
        return int(code)
    if signal.isdigit() and int(signal) != 0:
        return int(signal)
    return int(code) if code.isdigit() else None
