"""Generate a scheduler job script from a single COMMAND, so users stop
hand-writing one ``worker.sh`` per experiment.

Every hand-rolled HPC worker script did the same thing: declare ``#PBS``
directives, set up the environment, then ``chefe run`` one python entry point.
The whole environment story now lives in ``.chefe/activate.sh`` (chefe writes it:
module init + ``module purge`` + ``module load <pinned>`` + the pixi env), so the
generated job simply sources that file and runs the command. When it is absent
(a host chefe never installed) the body falls back to a plain ``chefe run env
PYTHONPATH=...``. The script text itself lives in jinja templates under
``lote/scripts`` (``pbs_job.sh.j2`` adds the ``#PBS`` header; ``bash_job.sh.j2``
is the same body without it, for pueue/bare hosts).
"""

import shlex

from jinja2 import Environment, PackageLoader

from . import NAME
from .base import FrozenModel

# PYTHONPATH the research entry points expect (repo root + the package src tree).
# Every branch of the generated body honours it: the activate.sh branch prepends it to
# whatever activate.sh exported, and the two fallbacks set it outright.
DEFAULT_PYTHONPATH = "research:research/projects/compression/src"

# The job script templates shipped with the package. ``trim_blocks``/``lstrip_blocks``
# make the `{% %}` control lines vanish from the rendered shell text;
# ``keep_trailing_newline`` keeps the scripts newline-terminated like any shell file.
TEMPLATES = Environment(
    loader=PackageLoader(NAME, "scripts"),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


class JobSpec(FrozenModel):
    """A ``--cmd`` job: one command plus the knobs a generated script needs.

    Carries what ``lote submit --cmd`` / ``lote run`` collected so the dispatcher can
    render the right script after it resolves the host's scheduler kind -- a PBS
    script with a ``#PBS`` header, or a plain bash wrapper for a pueue/bare host.

    cmd: the command to run (e.g. ``python -m projects...run --model X``).
    queue/walltime/select/gpus: PBS header values (ignored when rendering a bash wrapper).
    account: PBS ``group_list``; emitted as ``#PBS -W group_list=`` only when set.
    mem_gb: system memory to request in GB; joins the PBS ``select=`` chunk as ``mem=NNgb`` so a
        memory-hungry job is scheduled with the headroom it needs instead of being OOM-killed.
    pythonpath: ``PYTHONPATH`` the job runs under; prepended to activate.sh's own in the
        primary branch, and set outright in the activate.sh-absent fallbacks.
    """

    cmd: str
    queue: str = "debug-g"
    walltime: str = "00:30:00"
    select: int = 1
    gpus: int = 0
    account: str = ""
    mem_gb: int | None = None
    pythonpath: str = DEFAULT_PYTHONPATH

    def render(self, *, pbs: bool, gpu_in_select: bool = True) -> str:
        """The job script text: a full PBS script when ``pbs``, else a bash wrapper.

        The PBS header always carries ``-j oe`` (so ``lote logs`` finds the merged output) and tees
        all output to ``.lote/logs/<bare jobid>.log``. ``ngpus`` joins the ``select=`` chunk only
        when ``gpus`` > 0 and ``gpu_in_select``; some GPU queues (Miyabi ``debug-g``) hand the GPU
        out with the queue and reject an explicit ``ngpus``, so that host clears ``gpu_in_select``.
        ``mem=NNgb`` joins the same chunk when ``mem_gb`` is set, requesting the memory headroom.
        PBS enforces ``walltime`` itself; the bash/pueue wrapper has no scheduler, so it re-execs
        under ``timeout`` of the same budget, so a hung job cannot run forever and hold the slot.
        """
        template = TEMPLATES.get_template("pbs_job.sh.j2" if pbs else "bash_job.sh.j2")
        ngpus = f":ngpus={self.gpus}" if self.gpus and gpu_in_select else ""
        mem = f":mem={self.mem_gb}gb" if self.mem_gb else ""
        chunk = f"select={self.select}{ngpus}{mem}"
        return template.render(
            cmd=self.cmd,
            queue=self.queue,
            walltime=self.walltime,
            walltime_seconds=walltime_seconds(self.walltime),
            chunk=chunk,
            account=self.account,
            pythonpath=shlex.quote(self.pythonpath),
        )


def walltime_seconds(walltime: str) -> int:
    """A PBS ``HH:MM:SS`` walltime as whole seconds, for the bash host's ``timeout`` wrapper."""
    hours, minutes, seconds = (int(part) for part in walltime.split(":"))
    return hours * 3600 + minutes * 60 + seconds
