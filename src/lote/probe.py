# In-env host probe for lote onboarding. Runs on an already-synced host inside
# its pixi env, so it shares the lote models + psutil, and prints the host as a
# Target JSON the laptop reads back with `Target.model_validate`. The repo root
# is found by the caller before the sync, so it is passed in, not discovered here.

from __future__ import annotations

import grp
import os
import shutil
import subprocess

import fire
import psutil

from .models import Target


def stdout(*command: str) -> str:
    """Stdout of ``command``, or "" if the tool is missing or fails."""
    try:
        return subprocess.run(command, capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gpu() -> tuple[str | None, int | None]:
    """``(name, memory_MiB)`` of the first GPU via ``nvidia-smi``, or ``(None, None)``."""
    line = stdout("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits")
    if not line.strip():
        return None, None
    name, _, mem = line.splitlines()[0].partition(",")
    return name.strip() or None, int(mem) if mem.strip().isdigit() else None


def interactive_queue(kind: str) -> str | None:
    """The PBS queue whose name mentions "interact" (token-free on HPC), if any."""
    if kind != "pbs":
        return None
    rows = (row.split() for row in stdout("qstat", "-Q").splitlines()[2:])
    return next((row[0] for row in rows if row and "interact" in row[0].lower()), None)


def probe(name: str, root: str) -> None:
    """Print the host as a ``Target`` JSON (``name`` and ``root`` come from the caller)."""
    kind = "slurm" if shutil.which("sbatch") else "pbs" if shutil.which("qsub") else "ssh"
    gpu_name, gpu_mem_mb = gpu()
    print(
        Target(
            name=name,
            root=root,
            kind=kind,
            gpu_name=gpu_name,
            gpu_mem_mb=gpu_mem_mb,
            sysmem_gb=round(psutil.virtual_memory().total / 1024**3),
            account=grp.getgrgid(os.getgid()).gr_name,
            queue=interactive_queue(kind),
        ).model_dump_json()
    )


if __name__ == "__main__":
    fire.Fire(probe)
