import shlex

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lote.clients.pbs import (
    DependencyType,
    JobDependency,
    ResourceSpec,
    build_qsub_command,
    qdel,
    qstat,
)
from lote.clients.pbs.qsub import qsub
from lote.clients.rsync import Rsync, rsync
from lote.clients.slurm import build_sacct_command, build_scancel_command, build_squeue_command
from lote.clients.slurm.sbatch import build_sbatch_command

from .strategies import RSYNC_FLAGS

# --- PBS qsub builder ---


def test_build_qsub_command_threads_select_and_walltime() -> None:
    """A select clause, walltime, name, merged output and a var list all land in argv order."""
    command = build_qsub_command(
        script="/jobs/x.sh",
        queue="gen-S",
        group_list="grp",
        select=2,
        walltime="01:00:00",
        job_name="x",
        stdout_path="/logs/",
        join_output=True,
        variable_list={"ARGS": "--lr 0.1"},
        export_all_vars=False,
    )
    assert command[:5] == ["qsub", "-q", "gen-S", "-W", "group_list=grp"]
    assert "-l" in command and "select=2" in command[command.index("-l") + 1]
    assert "walltime=01:00:00" in command
    assert command[command.index("-N") + 1] == "x"
    assert command[command.index("-j") + 1] == "oe"
    assert command[command.index("-v") + 1] == "ARGS=--lr 0.1"
    assert command[-1] == "/jobs/x.sh"  # script is last
    assert "-V" not in command  # export_all_vars=False


def test_build_qsub_command_dependency_and_export() -> None:
    """A JobDependency renders to `depend=` and export_all_vars adds `-V`."""
    command = build_qsub_command(
        queue="q",
        group_list="g",
        select=1,
        dependency=JobDependency(kind=DependencyType.AFTER_OK, job_ids=["1", "2"]),
        export_all_vars=True,
        rerunnable=False,
    )
    assert "depend=afterok:1:2" in command
    assert "-V" in command
    assert command[command.index("-r") + 1] == "n"


def test_build_qsub_command_omits_empty_select_clause() -> None:
    """With every chunk field unset the select clause is empty, so no leading `-l select=`."""
    command = build_qsub_command(queue="q", group_list="g", select=None)  # type: ignore[arg-type]
    assert "select=" not in " ".join(command)


def test_resource_list_overrides_scalar_args() -> None:
    """A passed ResourceSpec wins field-by-field over the scalar select/walltime args."""
    command = build_qsub_command(
        queue="q",
        group_list="g",
        select=1,
        walltime="01:00:00",
        resource_list=ResourceSpec(select=8, ncpus=16, walltime="04:00:00", place="scatter"),
    )
    select_clause = command[command.index("-l") + 1]
    assert "select=8" in select_clause and "ncpus=16" in select_clause
    assert "walltime=04:00:00" in command
    assert "place=scatter" in command


def test_qsub_dry_run_renders_command(stub_bin: dict[str, str]) -> None:
    """`qsub(dry_run=True)` returns the shell-joined command without running anything."""
    rendered = qsub(script="/jobs/x.sh", queue="q", group_list="g", select=1, dry_run=True)
    assert rendered.startswith("qsub -q q -W group_list=g")
    assert rendered.endswith("/jobs/x.sh")


def test_qstat_dry_run_builds_flags(stub_bin: dict[str, str]) -> None:
    """`qstat` flag wiring: history `-H`, full `-f`, queue `-Q`, and trailing job ids."""
    rendered = qstat("123", full_output=True, history=True, queue="gpu", dry_run=True)
    assert rendered == "qstat -f -H -Q gpu 123"


def test_qdel_dry_run_force(stub_bin: dict[str, str]) -> None:
    """`qdel(force=True)` adds `-W force` before the ids."""
    assert qdel(["1", "2"], force=True, dry_run=True) == "qdel -W force 1 2"


def test_resource_spec_select_and_extra_clauses() -> None:
    """to_select_clause folds chunk fields; extra_clauses emits only the set non-select ones."""
    spec = ResourceSpec(select=2, ncpus=4, mem="32gb", walltime="01:00:00", place="pack")
    assert spec.to_select_clause() == "select=2:ncpus=4:mem=32gb"
    assert spec.extra_clauses() == ["walltime=01:00:00", "place=pack"]


def test_job_dependency_string() -> None:
    """A dependency with no ids is just its kind; with ids it colon-joins them."""
    assert JobDependency(kind=DependencyType.AFTER).to_pbs_string() == "after"
    assert (
        JobDependency(kind=DependencyType.AFTER_OK, job_ids=["7"]).to_pbs_string() == "afterok:7"
    )


# --- SLURM builders ---


def test_build_sbatch_command_omits_unset_and_orders_flags() -> None:
    """Only set resource flags appear; export prepends ALL; script is last."""
    command = build_sbatch_command(
        script="/jobs/x.sh",
        gpus=2,
        walltime="01:00:00",
        partition="gpu",
        mem_gb=64,
        job_name="x",
        output_path="/logs/%j.log",
        export_vars={"ARGS": "--n 1"},
    )
    assert command[0] == "sbatch"
    assert "--gpus=2" in command
    assert "--time=01:00:00" in command
    assert "--partition=gpu" in command
    assert "--mem=64G" in command
    assert "--export=ALL,ARGS=--n 1" in command
    assert "--account" not in " ".join(command)  # account unset -> absent
    assert command[-1] == "/jobs/x.sh"


def test_build_sbatch_command_drops_zero_gpus() -> None:
    """`gpus=0` is falsy, so no `--gpus` flag is emitted."""
    assert not any(c.startswith("--gpus") for c in build_sbatch_command(gpus=0))


def test_build_squeue_command_format_and_filters() -> None:
    """squeue requests the pipe format and threads `--me`/`--job`."""
    command = build_squeue_command(me=True, job_id="42")
    assert "--noheader" in command and "--format=%i|%j|%T|%P|%M" in command
    assert "--me" in command and command[command.index("--job") + 1] == "42"


def test_build_sacct_command_format() -> None:
    """sacct requests parseable, header-less rows of the fixed format."""
    command = build_sacct_command("123")
    assert command == [
        "sacct",
        "--jobs",
        "123",
        "--format=JobID,State,ExitCode",
        "--parsable2",
        "--noheader",
    ]


def test_build_scancel_command() -> None:
    """scancel takes one or many ids."""
    assert build_scancel_command("1") == ["scancel", "1"]
    assert build_scancel_command(["1", "2"]) == ["scancel", "1", "2"]


# --- rsync argv composition (StrFlag -> argv) ---


def test_rsync_merges_short_flags_and_keeps_long(stub_bin: dict[str, str]) -> None:
    """Single-letter flags merge into one `-azR` group; long flags stay separate."""
    command = rsync(
        "src/",
        "host:/dst/",
        Rsync.ARCHIVE | Rsync.COMPRESS | Rsync.RELATIVE | Rsync.DELETE,
        exclude=[".git/"],
        rsh="ssh -p 2222",
        bwlimit=1000,
        run=False,
    )
    parts = shlex.split(command)
    assert "-azR" in parts
    assert "--delete" in parts
    assert parts[parts.index("-e") + 1] == "ssh -p 2222"
    assert "--bwlimit=1000" in parts
    assert parts[parts.index("--exclude") + 1] == ".git/"
    assert command.rstrip().endswith("src/ host:/dst/")


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(flags=st.lists(RSYNC_FLAGS, min_size=1, max_size=6, unique=True))
def test_rsync_short_group_holds_exactly_the_short_members(
    flags: list[Rsync], stub_bin: dict[str, str]
) -> None:
    """The merged short group contains exactly the single-letter members' letters, in order."""
    command = rsync("a", "b", flags, run=False)
    shorts = [f for f in flags if len(f.string) == 2]
    longs = [f for f in flags if f.string.startswith("--")]
    if shorts:
        group = "-" + "".join(f.string[1] for f in shorts)
        assert group in command.split()
    for member in longs:
        assert member.string in command.split()
