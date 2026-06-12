from pydantic import ConfigDict

from ..base import FrozenModel
from ..environment import Environment
from .node import NodeClass


class Target(FrozenModel):
    """A resolved target: ssh alias + dispatch identity + per-class capabilities.

    What a host *is* (scheduler kind, repo root, account) lives here; what its
    machines *have* lives in ``classes``, one :class:`NodeClass` per node
    class. The ``login`` class comes from the stock over-ssh probe and every
    other class is a scheduler queue probed by a minimal submitted job, so an
    HPC host's GPU queues count for routing even though its login node has no
    GPU at all.

    name: the ``~/.ssh/config`` Host alias (also the ssh destination).
    kind: ``ssh`` (no scheduler, pueue), ``pbs`` (qsub), or ``slurm`` (sbatch).
    root: remote monorepo path.
    account: charging account / PBS ``group_list``, discovered as the user's group.
    queue: the host's interactive queue, when one was discovered.
    login_shell: run commands under ``bash -lc`` so an HPC host's ``/etc/profile.d``
        puts its scheduler toolchain on PATH (drives ``Environment.login``).
    classes: probed capabilities per node class, keyed by class name.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    kind: str = "ssh"
    root: str = "~/projects"
    account: str | None = None
    queue: str | None = None
    login_shell: bool = True
    classes: dict[str, NodeClass] = {}

    @property
    def environment(self) -> Environment:
        """The activation context for this host (root + login-shell choice)."""
        return Environment(root=self.root, login=self.login_shell)

    @property
    def best(self) -> NodeClass | None:
        """The most capable node class, or None before probing.

        GPU classes outrank CPU-only ones (a big-RAM login node must not eclipse
        the H100 queue jobs actually run on); memory breaks the tie.
        """
        sized = [node for node in self.classes.values() if node.vram_gb is not None]
        if not sized:
            return None
        return max(sized, key=lambda node: (node.gpu_name is not None, node.vram_gb or 0.0))

    @property
    def arch(self) -> str | None:
        """Short GPU arch label of the most capable node class."""
        return self.best.arch if self.best else None

    @property
    def vram_gb(self) -> float | None:
        """Usable memory in GB of the most capable node class."""
        return self.best.vram_gb if self.best else None

    def fits(self, needs_gb: float) -> bool:
        """Whether some node class of this target satisfies ``needs_gb``.

        needs_gb: requested memory in GB.
        """
        return self.vram_gb is not None and self.vram_gb >= needs_gb
