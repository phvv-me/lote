"""Generate a scheduler job script from a single COMMAND, so users stop
hand-writing one ``worker.sh`` per experiment.

Every hand-rolled HPC worker script did the same thing: declare ``#PBS``
directives, set up the environment, then ``chefe run`` one python entry point.
The whole environment story now lives in ``.chefe/activate.sh`` (chefe writes it:
module init + ``module purge`` + ``module load <pinned>`` + the pixi env), so the
generated job simply sources that file and runs the command. When it is absent
(a host chefe never installed) the body falls back to a plain ``chefe run``. The
script text itself lives in jinja templates under
``lote/scripts`` (``pbs_job.sh.j2`` adds the ``#PBS`` header; ``bash_job.sh.j2``
is the same body without it, for pueue/bare hosts).
"""

import shlex

from jinja2 import Environment, PackageLoader

from . import NAME
from .base import FrozenModel

# The job script templates shipped with the package. ``trim_blocks``/``lstrip_blocks``
# make the `{% %}` control lines vanish from the rendered shell text;
# ``keep_trailing_newline`` keeps the scripts newline-terminated like any shell file.
TEMPLATES = Environment(
    loader=PackageLoader(NAME, "scripts"),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


# The walltime a PBS header falls back to when the caller chose none: sized to the default
# ``debug-g`` queue's 30-minute cap, and always echoed by the dispatcher's submit log so the
# cap is never silent.
PBS_DEFAULT_WALLTIME = "00:30:00"


class JobSpec(FrozenModel):
    """A ``--cmd`` job: one command plus the knobs a generated script needs.

    Carries what ``lote submit --cmd`` / ``lote run`` collected so the dispatcher can
    render the right script after it resolves the host's scheduler kind -- a PBS
    script with a ``#PBS`` header, or a plain bash wrapper for a pueue/bare host.

    Walltime semantics differ by backend, deliberately. A PBS queue always enforces a
    walltime, so its header gets ``walltime`` or the ``debug-g``-sized default. A
    schedulerless host (pueue/bash) enforces a cap only when the caller explicitly chose
    one: the old behavior of silently applying the 30-minute default there SIGTERM-killed
    healthy long runs, and an invisible default that kills correct work is worse than a
    hung job the monitor can see and ``lote kill`` can stop. When a cap is set, the
    wrapper stamps ``lote: killed at walltime HH:MM:SS`` into the log so ``lote why``
    decodes the stop.

    cmd: the command to run (e.g. ``python -m projects...run --model X``).
    queue/select/gpus: PBS header values (ignored when rendering a bash wrapper).
    walltime: ``HH:MM:SS`` cap; None means the PBS default header and no cap on a
        schedulerless host.
    account: PBS ``group_list``; emitted as ``#PBS -W group_list=`` only when set.
    mem_gb: system memory to request in GB; joins the PBS ``select=`` chunk as ``mem=NNgb`` so a
        memory-hungry job is scheduled with the headroom it needs instead of being OOM-killed.
    pythonpath: explicit ``PYTHONPATH`` the job runs under, empty for an isolated default.
    """

    cmd: str
    queue: str = "debug-g"
    walltime: str | None = None
    select: int = 1
    gpus: int = 0
    account: str = ""
    mem_gb: int | None = None
    pythonpath: str = ""

    @property
    def pbs_walltime(self) -> str:
        """The walltime a PBS header carries: the explicit cap, else the debug-queue default."""
        return self.walltime or PBS_DEFAULT_WALLTIME

    def render(self, *, pbs: bool, gpu_in_select: bool = True) -> str:
        """The job script text: a full PBS script when ``pbs``, else a bash wrapper.

        The PBS header always carries ``-j oe`` and redirects merged output directly into
        ``.lote/logs/<bare jobid>.log``. Its exit trap appends the final status after that output
        and writes the same status to ``.lote/logs/<bare jobid>.exit`` so a job the server later
        purges can still be autopsied.
        ``ngpus`` joins the ``select=`` chunk only when ``gpus`` > 0 and ``gpu_in_select``; some
        GPU queues (Miyabi ``debug-g``) hand the GPU out with the queue and reject an explicit
        ``ngpus``, so that host clears ``gpu_in_select``. ``mem=NNgb`` joins the same chunk when
        ``mem_gb`` is set. PBS enforces its walltime itself; the bash/pueue wrapper runs under
        ``timeout`` only when the caller chose a walltime, stamping a clear kill verdict into the
        log when the budget hits (see the class docstring for why the default is uncapped there).
        """
        template = TEMPLATES.get_template("pbs_job.sh.j2" if pbs else "bash_job.sh.j2")
        ngpus = f":ngpus={self.gpus}" if self.gpus and gpu_in_select else ""
        mem = f":mem={self.mem_gb}gb" if self.mem_gb else ""
        chunk = f"select={self.select}{ngpus}{mem}"
        return template.render(
            cmd=shlex.quote(self.cmd),
            queue=self.queue,
            walltime=self.pbs_walltime if pbs else self.walltime,
            walltime_seconds=walltime_seconds(self.walltime) if self.walltime else 0,
            chunk=chunk,
            account=self.account,
            pythonpath=shlex.quote(self.pythonpath) if self.pythonpath else "",
        )


def walltime_seconds(walltime: str) -> int:
    """A PBS ``HH:MM:SS`` walltime as whole seconds, for the bash host's ``timeout`` wrapper."""
    hours, minutes, seconds = (int(part) for part in walltime.split(":"))
    return hours * 3600 + minutes * 60 + seconds
