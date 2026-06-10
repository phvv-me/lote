"""Generate a scheduler job script from a single COMMAND, so users stop
hand-writing one ``worker.sh`` per experiment.

Every hand-rolled HPC worker script did the same thing: declare ``#PBS``
directives, set up the environment, then ``chefe run`` one python entry point.
The whole environment story now lives in ``.chefe/activate.sh`` (chefe writes it:
module init + ``module purge`` + ``module load <pinned>`` + the pixi env), so the
generated job simply sources that file and runs the command. When it is absent
(a host chefe never installed) the body falls back to a plain ``chefe run env
PYTHONPATH=...``. :func:`render_pbs_job` adds the ``#PBS`` header;
:func:`render_bash_job` is the same body without it, for pueue/bare hosts.
"""

from __future__ import annotations

import shlex

# PYTHONPATH the research entry points expect (repo root + the package src tree).
# Only the activate.sh-absent fallback needs it; activate.sh sets PYTHONPATH itself.
DEFAULT_PYTHONPATH = "research:research/projects/compression/src"

# The generated job body, most-self-contained first: source chefe's `.chefe/activate.sh` when
# present (it provides the HPC modules + the pixi env + PYTHONPATH). If absent, run the pixi env's
# own python directly -- a host with a built env but a missing or out-of-date `chefe` still runs,
# with no `chefe` invoked at job time. Only when neither exists fall back to `chefe run`.
ACTIVATE = ".chefe/activate.sh"
ENV_BIN = ".chefe/.pixi/envs/default/bin"  # the env chefe builds; its python needs no chefe to run


def _job_body(cmd: str, pythonpath: str) -> str:
    """The shared run body: activate.sh, else the pixi env python directly, else `chefe run`."""
    quoted = shlex.quote(pythonpath)
    return (
        f"if [ -f {ACTIVATE} ]; then\n"
        f"  source {ACTIVATE}\n"
        f"  {cmd}\n"
        f'elif [ -x "{ENV_BIN}/python" ]; then\n'
        f'  PATH="$PWD/{ENV_BIN}:$PATH" PYTHONPATH={quoted} {cmd}\n'
        "else\n"
        f"  chefe run env PYTHONPATH={quoted} {cmd}\n"
        "fi\n"
    )


def render_pbs_job(
    cmd: str,
    *,
    queue: str = "debug-g",
    walltime: str = "00:30:00",
    select: int = 1,
    gpus: int = 0,
    pythonpath: str = DEFAULT_PYTHONPATH,
) -> str:
    """Render a complete PBS job script that runs ``cmd`` on a compute node.

    Assembles the ``#PBS`` header (from the flags, always ``-j oe`` so ``lote logs``
    finds the merged output), a ``set -euo pipefail`` + ``cd $PBS_O_WORKDIR`` guard,
    the tee that keys the log to the bare PBS job id, then the shared body that
    sources ``.chefe/activate.sh`` and runs ``cmd``.

    cmd: the command to run, e.g. ``python -m projects...run --model X``.
    queue: PBS queue (``-q``).
    walltime: ``HH:MM:SS`` cap (``-l walltime=``).
    select: node/chunk count (``-l select=``).
    gpus: GPUs per chunk; appended as ``:ngpus=<gpus>`` only when > 0. Default 0 -- many GPU
        queues (Miyabi ``debug-g``) provide the GPU implicitly and reject ``ngpus`` in ``select``.
    pythonpath: ``PYTHONPATH`` for the activate.sh-absent fallback.
    """
    chunk = f"select={select}" + (f":ngpus={gpus}" if gpus else "")
    return (
        "#!/bin/bash\n"
        f"#PBS -q {queue}\n"
        f"#PBS -l {chunk}\n"
        f"#PBS -l walltime={walltime}\n"
        "#PBS -j oe\n"
        "set -euo pipefail\n"
        'cd "${PBS_O_WORKDIR:-$PWD}"\n'
        # Tee all output to a path keyed by the bare PBS job id (== the lote handle), so
        # `lote logs <handle>` finds it regardless of where PBS spools its own .o<id> file.
        "mkdir -p .lote/logs\n"
        'exec > >(tee ".lote/logs/${PBS_JOBID%%.*}.log") 2>&1\n'
        f"{_job_body(cmd, pythonpath)}"
    )


def render_bash_job(cmd: str, *, pythonpath: str = DEFAULT_PYTHONPATH) -> str:
    """Render a plain bash wrapper for a non-scheduler host (pueue / bare bash).

    The same body as :func:`render_pbs_job` minus the ``#PBS`` header: it sources
    ``.chefe/activate.sh`` when present, so one ``--cmd`` path covers every host kind.
    """
    return "#!/bin/bash\nset -euo pipefail\n" + _job_body(cmd, pythonpath)
