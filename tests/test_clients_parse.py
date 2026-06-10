import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lote.clients.pbs import PbsState, parse_qstat_full, parse_qstat_output
from lote.clients.pbs._common import extract_job_id, parse_job_state, parse_variable_list
from lote.clients.pueue.state import PueueState
from lote.clients.slurm import (
    SlurmState,
    parse_sacct_output,
    parse_squeue_output,
)
from lote.clients.slurm._common import extract_job_id as slurm_extract_job_id
from lote.clients.slurm._common import parse_exit_code, parse_slurm_state

from .strategies import job_infos, slurm_jobs

# --- PBS qstat ---

STANDARD_QSTAT = """\
Job ID            Name        User    Time Use S Queue
----------------  ----------  ------  -------- - -----
123.pbs           train       alice   00:10:00 R gpu
456.pbs           eval        bob     00:00:00 Q gen-S
"""

WIDE_QSTAT = """\
JOB_ID JOB_NAME STATUS PROJECT QUEUE START_DATE ELAPSE TOKEN NODE MIG
123 train RUNNING proj gpu 2024-01-01 12:00 01:00:00 t 1 0
456 eval QUEUED proj gen-S 2024-01-01 --:--:-- t 1 0
"""


def test_parse_qstat_standard_layout() -> None:
    """Standard PBS layout parses id/name/user/state/queue and the walltime-used column."""
    jobs = parse_qstat_output(STANDARD_QSTAT)
    assert [j.job_id for j in jobs] == ["123.pbs", "456.pbs"]
    assert jobs[0].state is PbsState.RUNNING
    assert jobs[0].walltime_used == "00:10:00"
    assert jobs[1].queue == "gen-S"


def test_parse_qstat_wide_vendor_layout() -> None:
    """The wide vendor layout folds the two-token START_DATE and word states correctly."""
    jobs = parse_qstat_output(WIDE_QSTAT)
    assert jobs[0].job_id == "123" and jobs[0].state is PbsState.RUNNING
    assert jobs[0].project == "proj"
    assert jobs[0].resources_used["start_date"] == "2024-01-01 12:00"
    assert jobs[1].state is PbsState.QUEUED
    assert jobs[1].walltime_used is None  # the `--:--:--` sentinel becomes None


def test_parse_qstat_no_header_is_empty() -> None:
    """Output without a recognisable header yields no jobs (not a crash)."""
    assert parse_qstat_output("garbage\nmore garbage\n") == []


def test_parse_qstat_full_record() -> None:
    """`qstat -f` key=value blocks map onto JobInfo fields and the resource sub-dicts."""
    text = """\
Job Id: 789.pbs
    Job_Name = bigjob
    Job_Owner = alice@login01
    job_state = F
    queue = gpu
    project = myproj
    Resource_List.walltime = 02:00:00
    Resource_List.select = 1:ncpus=8
    resources_used.walltime = 01:55:00
    Variable_List = ARGS=--lr 0.1,FOO=bar
    Exit_status = 0
"""
    [job] = parse_qstat_full(text)
    assert job.job_id == "789.pbs"
    assert job.name == "bigjob"
    assert job.user == "alice"  # @host stripped
    assert job.state is PbsState.FINISHED
    assert job.walltime == "02:00:00"
    assert job.resources_requested["select"] == "1:ncpus=8"
    assert job.walltime_used == "01:55:00"
    assert job.variables["ARGS"] == "--lr 0.1"


def test_parse_qstat_full_handles_multiple_records() -> None:
    """Two `Job Id:` blocks parse as two jobs; output with no block at all is empty."""
    text = "Job Id: 1.s\n    job_state = R\nJob Id: 2.s\n    job_state = Q\n"
    jobs = parse_qstat_full(text)
    assert [j.job_id for j in jobs] == ["1.s", "2.s"]
    assert parse_qstat_full("no job-id header here\n") == []  # never opens a record


@given(st.lists(job_infos(), min_size=1, max_size=4))
def test_parse_qstat_full_roundtrips_job_ids(jobs: list[object]) -> None:
    """Rendering N JobInfo as `qstat -f` blocks parses back to the same id sequence."""
    from lote.clients.pbs import JobInfo

    typed: list[JobInfo] = jobs  # type: ignore[assignment]
    text = "".join(f"Job Id: {job.job_id}\n    job_state = {job.state}\n" for job in typed)
    assert [job.job_id for job in parse_qstat_full(text)] == [job.job_id for job in typed]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123.pbs", "123"),
        ("456[].pbs", "456[]"),
        ("789", "789"),
        ("not-a-number\nline2", "not-a-number\nline2"),
    ],
)
def test_extract_job_id(raw: str, expected: str) -> None:
    """The numeric (optionally array) job id is pulled out of qsub output; else verbatim."""
    assert extract_job_id(raw) == expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [("R", PbsState.RUNNING), ("RUNNING", PbsState.RUNNING), ("begun", PbsState.ARRAY_BEGUN)],
)
def test_parse_job_state_letters_and_words(token: str, expected: PbsState) -> None:
    """Single letters and full words both resolve to the canonical state."""
    assert parse_job_state(token) == expected


def test_parse_job_state_unknown_passthrough() -> None:
    """An unknown token is returned verbatim (display-only)."""
    assert parse_job_state("ZZZ") == "ZZZ"


def test_parse_variable_list() -> None:
    """A PBS Variable_List splits on commas and keeps the first `=`."""
    assert parse_variable_list("A=1,B=x=y,bad") == {"A": "1", "B": "x=y"}


# --- SLURM ---

SQUEUE = "123|train|RUNNING|gpu|00:10:00\n456|eval|PENDING||\n"


def test_parse_squeue_output() -> None:
    """Pipe-delimited squeue rows parse, with empty partition/elapsed becoming None."""
    jobs = parse_squeue_output(SQUEUE)
    assert jobs[0].job_id == "123" and jobs[0].state is SlurmState.RUNNING
    assert jobs[0].partition == "gpu" and jobs[0].elapsed == "00:10:00"
    assert jobs[1].partition is None and jobs[1].elapsed is None


def test_parse_squeue_skips_short_rows() -> None:
    """A row with too few fields is dropped rather than crashing the parse."""
    assert parse_squeue_output("123|train\n") == []


def test_parse_sacct_keeps_top_level_row() -> None:
    """sacct parse keeps the `<id>` row and ignores `.batch`/`.extern` sub-steps."""
    output = "123|COMPLETED|0:0\n123.batch|COMPLETED|0:0\n123.extern|COMPLETED|0:0\n"
    job = parse_sacct_output(output, "123")
    assert job is not None
    assert job.state is SlurmState.COMPLETED and job.exit_code == 0


def test_parse_sacct_absent_is_none() -> None:
    """A job missing from the accounting db parses to None (vanished)."""
    assert parse_sacct_output("999|FAILED|1:0\n", "123") is None


def test_parse_slurm_state_strips_cancelled_suffix() -> None:
    """`CANCELLED by 1000` resolves to CANCELLED; unknowns pass through."""
    assert parse_slurm_state("CANCELLED by 1000") is SlurmState.CANCELLED
    assert parse_slurm_state("WHAT") == "WHAT"


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("0:0", 0),
        ("2:0", 2),  # non-zero return code wins
        ("0:9", 9),  # killed by signal
        ("", None),
        ("x:y", None),
    ],
)
def test_parse_exit_code(field: str, expected: int | None) -> None:
    """ExitCode `<code>:<signal>`: return code, else signal, else None."""
    assert parse_exit_code(field) == expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Submitted batch job 12345", "12345"),
        ("noise\nSubmitted batch job 678\n", "678"),
        ("90210\n", "90210"),  # bare --parsable id
        ("", ""),
    ],
)
def test_slurm_extract_job_id(output: str, expected: str) -> None:
    """sbatch's `Submitted batch job N` line, or a trailing bare id, is extracted."""
    assert slurm_extract_job_id(output) == expected


@given(slurm_jobs())
def test_squeue_roundtrip(job: object) -> None:
    """A SlurmJob rendered as a squeue line parses back to the same id/name/partition/elapsed."""
    from lote.clients.slurm import SlurmJob

    typed: SlurmJob = job  # type: ignore[assignment]
    line = (
        f"{typed.job_id}|{typed.name}|{typed.state}|{typed.partition or ''}|{typed.elapsed or ''}"
    )
    [parsed] = parse_squeue_output(line)
    assert parsed.job_id == typed.job_id
    assert parsed.name == typed.name
    assert parsed.partition == (typed.partition or None)
    assert parsed.elapsed == (typed.elapsed or None)


# --- pueue ---


def _pueue_status(tasks: list[dict[str, object]]) -> str:
    return json.dumps({"tasks": {str(i): t for i, t in enumerate(tasks)}})


def test_pueue_state_values_match_tags() -> None:
    """Every PueueState's value is the externally-tagged status key pueue emits."""
    assert PueueState("Running") is PueueState.RUNNING
    assert PueueState("Done") is PueueState.DONE
