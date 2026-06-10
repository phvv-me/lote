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
# Only the activate.sh-absent fallback needs it; activate.sh sets PYTHONPATH itself.
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
    pythonpath: ``PYTHONPATH`` for the activate.sh-absent fallback.
    """

    cmd: str
    queue: str = "debug-g"
    walltime: str = "00:30:00"
    select: int = 1
    gpus: int = 0
    account: str = ""
    pythonpath: str = DEFAULT_PYTHONPATH

    def render(self, *, pbs: bool) -> str:
        """The job script text: a full PBS script when ``pbs``, else a bash wrapper.

        The PBS header always carries ``-j oe`` (so ``lote logs`` finds the merged
        output) and tees all output to ``.lote/logs/<bare jobid>.log``; ``ngpus`` is
        appended to the ``select=`` chunk only when ``gpus`` > 0, since many GPU
        queues (Miyabi ``debug-g``) provide the GPU implicitly and reject it.
        """
        template = TEMPLATES.get_template("pbs_job.sh.j2" if pbs else "bash_job.sh.j2")
        chunk = f"select={self.select}" + (f":ngpus={self.gpus}" if self.gpus else "")
        return template.render(
            cmd=self.cmd,
            queue=self.queue,
            walltime=self.walltime,
            chunk=chunk,
            account=self.account,
            pythonpath=shlex.quote(self.pythonpath),
        )
