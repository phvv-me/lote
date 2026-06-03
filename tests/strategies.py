from __future__ import annotations

import string

from hypothesis import strategies as st

from fleet.clients.pbs import JobInfo, JobState
from fleet.clients.pueue.state import PueueState
from fleet.clients.pueue.task import PueueTask
from fleet.clients.rsync import Rsync
from fleet.clients.slurm import SlurmJob, SlurmState
from fleet.models import Target
from fleet.schedulers import Resources

# Leaf alphabets come from stdlib `string`; the structure of each model is derived from the
# model class itself via `st.builds`, so a new field on a model widens these strategies as
# soon as its leaf strategy is named here.

# A scheduler handle / job id: digits with an optional PBS `.server` suffix.
HANDLES = st.from_regex(r"[0-9]{1,7}", fullmatch=True)

# A job / task name: a short identifier the schedulers and logs key on.
NAMES = st.text(string.ascii_letters + string.digits + "_-", min_size=0, max_size=12)

# Queue / partition labels.
QUEUES = st.sampled_from(["gen-S", "regular", "gpu", "debug", "interactive"])

# Walltime / elapsed strings in HH:MM:SS.
DURATIONS = st.from_regex(r"[0-9]{2}:[0-5][0-9]:[0-5][0-9]", fullmatch=True)


def job_states() -> st.SearchStrategy[JobState | str]:
    """A PBS state: every known single-letter state, plus an unknown token (str fallback)."""
    return st.one_of(st.sampled_from(list(JobState)), st.just("X"))


def slurm_states() -> st.SearchStrategy[SlurmState | str]:
    """A SLURM state: every known long-form state, plus an unknown token (str fallback)."""
    return st.one_of(st.sampled_from(list(SlurmState)), st.just("WEIRD"))


def pueue_states() -> st.SearchStrategy[PueueState | str]:
    """A pueue lifecycle state, plus an unknown token (str fallback)."""
    return st.one_of(st.sampled_from(list(PueueState)), st.just("Mystery"))


def job_infos() -> st.SearchStrategy[JobInfo]:
    """A `JobInfo` built straight from the model, so new fields widen this for free."""
    return st.builds(
        JobInfo,
        job_id=HANDLES,
        name=NAMES,
        user=NAMES,
        state=job_states(),
        queue=QUEUES,
        walltime=st.none() | DURATIONS,
        walltime_used=st.none() | DURATIONS,
    )


def slurm_jobs() -> st.SearchStrategy[SlurmJob]:
    """A `SlurmJob` (squeue/sacct row) built from the model."""
    return st.builds(
        SlurmJob,
        job_id=HANDLES,
        name=NAMES,
        state=slurm_states(),
        exit_code=st.none() | st.integers(min_value=0, max_value=255),
        partition=st.none() | QUEUES,
        elapsed=st.none() | DURATIONS,
    )


def pueue_tasks() -> st.SearchStrategy[PueueTask]:
    """A `PueueTask` built from the model, covering live and terminal results."""
    return st.builds(
        PueueTask,
        id=st.integers(min_value=0, max_value=99999),
        label=st.none() | NAMES,
        state=pueue_states(),
        result=st.none() | st.sampled_from(["Success", "Killed", "Failed"]),
        exit_code=st.none() | st.integers(min_value=0, max_value=255),
    )


def resources() -> st.SearchStrategy[Resources]:
    """A scheduler-agnostic `Resources` request built from the model."""
    return st.builds(
        Resources,
        gpus=st.integers(min_value=0, max_value=8),
        walltime=st.none() | DURATIONS,
        queue=st.none() | QUEUES,
        account=st.none() | NAMES.filter(bool),
        mem_gb=st.none() | st.integers(min_value=1, max_value=512),
    )


def targets() -> st.SearchStrategy[Target]:
    """A resolved `Target` built from the model, spanning gpu and sysmem-only hosts."""
    return st.builds(
        Target,
        name=NAMES.filter(bool),
        kind=st.sampled_from(["ssh", "pbs", "slurm"]),
        gpu_name=st.none() | st.sampled_from(["NVIDIA GB10", "NVIDIA A100", "NVIDIA H100"]),
        gpu_mem_mb=st.none() | st.integers(min_value=1024, max_value=200000),
        sysmem_gb=st.none() | st.integers(min_value=1, max_value=2048),
    )


# Every single-member rsync flag; lists of these drive the argv-composition property.
RSYNC_FLAGS = st.sampled_from(list(Rsync))
