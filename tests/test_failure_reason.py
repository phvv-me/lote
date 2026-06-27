"""``failure_reason`` extracts the one-line cause a person would grep for, so ``lote why`` answers
"why did it fail" without reading the whole log. Cases are the real failures from a Miyabi session.
"""

import pytest

from lote.schedulers import exit_reason, failure_reason


def test_python_traceback_returns_the_raised_exception():
    """The last line of a traceback (the actual exception) is the reason, not the frame lines."""
    log = (
        "loading model...\n"
        '  File "rotation.py", line 65, in forward_rows\n'
        '  File "hadamard.py", line 37, in fwht\n'
        "ModuleNotFoundError: No module named 'fast_hadamard_transform'\n"
    )
    assert failure_reason(log) == "ModuleNotFoundError: No module named 'fast_hadamard_transform'"


def test_dotted_exception_name_is_matched():
    """A dotted exception (torch._C._LinAlgError) still reads as the cause."""
    log = "Traceback (most recent call last):\ntorch._C._LinAlgError: not positive-definite\n"
    assert failure_reason(log).startswith("torch._C._LinAlgError")


def test_scheduler_rejection_when_there_is_no_python_error():
    """A qsub rejection (no traceback) surfaces over generic noise."""
    log = "running setup...\nqsub: Resource invalid in \"select\" specification: ngpus\n"
    assert "ngpus" in failure_reason(log)
    assert failure_reason(log).lower().startswith("qsub")


def test_falls_back_to_last_nonempty_line():
    """With no recognizable marker, the last non-empty line is the best guess."""
    log = "step 1 ok\nstep 2 ok\njob killed by walltime\n\n"
    assert failure_reason(log) == "job killed by walltime"


def test_empty_log_is_handled():
    assert failure_reason("   \n\n") == "(no log output)"


def test_last_exception_wins_over_an_inner_one():
    """When the traceback re-raises, the final (outermost) exception is the reported cause."""
    log = "ValueError: inner\nDuring handling of the above, another arose:\nRuntimeError: outer\n"
    assert failure_reason(log) == "RuntimeError: outer"


def test_sigkill_exit_137_reads_as_oom_or_walltime_when_log_is_silent():
    """The 14B/32B SIGKILL (exit 137) leaves no traceback, so the exit code names the cause.

    A job the OOM killer or a hard walltime stop kills exits 137 (128 + SIGKILL) after printing
    perfectly healthy progress, so the last log line ("step 400 ok") would mislead. The exit code
    supplies "out of memory or walltime" instead.
    """
    log = "loading shards...\nstep 200 ok\nstep 400 ok\n"
    reason = failure_reason(log, exit_code=137)
    assert "memory" in reason and "walltime" in reason
    assert "step 400" not in reason


def test_timeout_exit_124_reads_as_walltime_exceeded():
    """GNU ``timeout`` (the pueue/bash walltime cap) exits 124, surfaced as a walltime stop."""
    assert "walltime" in failure_reason("warming up...\n", exit_code=124).lower()


def test_a_real_traceback_wins_over_the_exit_code():
    """A CUDA OOM that actually raised still reports the exception, not the generic signal line."""
    log = "training...\ntorch.cuda.OutOfMemoryError: CUDA out of memory.\n"
    assert failure_reason(log, exit_code=137).startswith("torch.cuda.OutOfMemoryError")


def test_a_plain_nonzero_exit_does_not_invent_a_signal_reason():
    """An ordinary failure (exit 1) with a log keeps the log-derived reason, not a signal note."""
    assert failure_reason("step 1 ok\nboom\n", exit_code=1) == "boom"


@pytest.mark.parametrize(
    ("code", "needle"),
    [(124, "walltime"), (137, "memory"), (139, "segfault"), (143, "SIGTERM")],
)
def test_exit_reason_maps_externally_imposed_codes(code: int, needle: str):
    reason = exit_reason(code)
    assert reason is not None and needle in reason


@pytest.mark.parametrize("code", [None, 0, 1, 2, 42])
def test_exit_reason_is_none_for_ordinary_codes(code: int | None):
    """Only the signal/timeout codes carry a reason; a plain exit returns None to fall through."""
    assert exit_reason(code) is None
