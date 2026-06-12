import string

from hypothesis import strategies as st

from lote.clients.pbs import JobInfo, PbsState
from lote.clients.pueue.state import PueueState
from lote.clients.pueue.task import PueueTask
from lote.clients.rsync import Rsync
from lote.clients.slurm import SlurmJob, SlurmState
from lote.models import LOGIN, NodeClass, Snapshot, Target
from lote.models.snapshot import CpuReading, GpuReading, MemoryReading
from lote.schedulers import Resources

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


def job_states() -> st.SearchStrategy[PbsState | str]:
    """A PBS state: every known single-letter state, plus an unknown token (str fallback)."""
    return st.one_of(st.sampled_from(list(PbsState)), st.just("X"))


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


def node_classes() -> st.SearchStrategy[NodeClass]:
    """A `NodeClass` built from the model, spanning gpu, cpu-only, and unprobed nodes."""
    return st.builds(
        NodeClass,
        name=st.just(LOGIN) | QUEUES,
        gpu_name=st.none() | st.sampled_from(["NVIDIA GB10", "NVIDIA A100", "NVIDIA H100"]),
        gpu_count=st.integers(min_value=0, max_value=8),
        gpu_mem_mb=st.none() | st.integers(min_value=1024, max_value=200000),
        sysmem_gb=st.none() | st.integers(min_value=1, max_value=2048),
        cpu_cores=st.none() | st.integers(min_value=1, max_value=512),
    )


def memory_readings() -> st.SearchStrategy[MemoryReading]:
    """A `MemoryReading` from zero (unreported) up to a few-TiB pool."""
    return st.builds(MemoryReading, total_bytes=st.integers(min_value=0, max_value=2**43))


def snapshots() -> st.SearchStrategy[Snapshot]:
    """A mainboard snapshot slice built from the models, spanning gpu and cpu-only nodes."""
    gpus = st.builds(
        GpuReading,
        unit_name=st.sampled_from(["", "NVIDIA H100", "NVIDIA GB10"]),
        memory=memory_readings(),
    )
    cpu = st.builds(CpuReading, name=NAMES, logical_cores=st.integers(min_value=0, max_value=512))
    return st.builds(
        Snapshot,
        hostname=NAMES,
        cpu=cpu,
        memory=memory_readings(),
        gpus=st.lists(gpus, max_size=4).map(tuple),
    )


def targets() -> st.SearchStrategy[Target]:
    """A resolved `Target` built from the model, spanning unprobed and multi-class hosts."""
    return st.builds(
        Target,
        name=NAMES.filter(bool),
        kind=st.sampled_from(["ssh", "pbs", "slurm"]),
        classes=st.dictionaries(st.just(LOGIN) | QUEUES, node_classes(), max_size=3),
    )


# Every single-member rsync flag; lists of these drive the argv-composition property.
RSYNC_FLAGS = st.sampled_from(list(Rsync))
