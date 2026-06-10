import shlex
import subprocess

from plumbum import SshMachine

from . import NAME
from .base import FrozenModel

USER_BINS = ("$HOME/.local/bin", "$HOME/.pixi/bin", "$HOME/.cargo/bin")


class Environment(FrozenModel):
    """How to activate a command on a host -- the single source of truth.

    Collapses the formerly divergent activation paths (the cli's cargo-only
    PATH insert, pueue's ``USER_BINS``, the schedulers' login-shell
    ``remote_exec``, and ``setup.sh``'s PATH) into one wrapper. Prepending the
    user bins on every path is what lets ``chefe`` resolve on any host instead of
    falling back to the pixi binary; a login shell additionally sources
    ``/etc/profile.d`` so an HPC host's ``qsub``/``module`` toolchain is on PATH.

    root: the repo path on the host (the working directory commands run from).
    login: wrap under ``bash -lc`` (sources the HPC toolchain) rather than ``bash -c``.
    user_bins: per-user install dirs prepended to PATH (chefe, pixi, cargo live here).
    """

    root: str
    login: bool = True
    user_bins: tuple[str, ...] = USER_BINS

    @property
    def path(self) -> str:
        """The user bins joined as a ``PATH`` prefix."""
        return ":".join(self.user_bins)

    def wrap(self, command: str, *, chefe: bool = True, cd: bool = True) -> str:
        """Activated body: ``cd <root> && export PATH=<bins>:$PATH && [chefe run] <command>``.

        command: the bare command to run on the host.
        chefe: run it through ``chefe run`` (inside the compiled env).
        cd: change into ``root`` first (pueue sets its own working directory, off there).
        """
        steps = [f"cd {shlex.quote(self.root)}"] if cd else []
        steps.append(f"export PATH={self.path}:$PATH")
        steps.append(f"chefe run {command}" if chefe else command)
        return " && ".join(steps)

    def argv(self, command: str, *, chefe: bool = True, cd: bool = True) -> list[str]:
        """``wrap`` under a ``bash`` login (``-lc``) or plain (``-c``) shell.

        Ready for plumbum (``machine[argv[0]][argv[1:]]``) or subprocess.
        """
        flag = "-lc" if self.login else "-c"
        return ["bash", flag, self.wrap(command, chefe=chefe, cd=cd)]

    def exec_command(self, *args: str) -> str:
        """Activated body for the on-host executor: ``chefe run lote exec <args>``.

        The single builder for every scheduler's ``lote exec qsub``/``sbatch``/
        ``run``/``status``/... call (the former ``_remote.remote_exec``); the
        caller wraps it in ``bash -lc`` so the login shell adds the cluster
        toolchain (``qsub``/``module``).
        """
        command = f"{NAME} exec " + " ".join(shlex.quote(str(arg)) for arg in args)
        return self.wrap(command, chefe=True)

    def connection(self, host: str) -> SshMachine:
        """Open an ssh connection to ``host`` with the user install dirs on PATH.

        The single place bare-tool PATH is set, so ``chefe``/``pueue``/
        ``nvidia-smi`` resolve from the same ``user_bins`` activated commands use --
        never the forbidden pixi binary. Replaces the cli's ``connect``.

        First warms the host's ssh ``ControlMaster`` (from ``~/.ssh/config``) with a
        throwaway subprocess ``ssh``: if the persistent master has expired, that slow
        relogin happens on a robust one-shot channel, so plumbum's persistent session
        then rides a live master instead of dying mid-handshake (the
        ``readline of closed file`` we hit on miyabi) during the reconnect. We do not
        set ``ControlMaster``/``ControlPath`` ourselves -- the user's config owns the
        multiplexing; overriding it would open a second, unauthenticated master.

        That same warm-up doubles as a host-key check: if it fails verification (the host
        or its ProxyJump rotated its key, or the entry is missing) we raise a clear,
        actionable error here instead of letting plumbum die with an opaque traceback.
        """
        warm = subprocess.run(["ssh", host, "true"], capture_output=True, text=True, check=False)
        if warm.returncode != 0 and "host key verification failed" in warm.stderr.lower():
            raise ConnectionError(
                f"ssh to {host!r} failed host-key verification -- the host or its ProxyJump "
                f"rotated its key, or the known_hosts entry is missing. Re-verify it "
                f"(`ssh {host} true`, accept the fingerprint or refresh known_hosts), then retry."
            )
        remote = SshMachine(host)
        for bindir in reversed(self.user_bins):
            remote.env.path.insert(0, remote.cwd / bindir.removeprefix("$HOME/"))
        return remote
