from __future__ import annotations

import shlex


def remote_exec(root: str, *args: str) -> str:
    """A login-shell ``cd <root> && chefe run fleet exec <args>`` for the host.

    The login shell (``bash -lc``) sources ``/etc/profile.d`` so the cluster
    toolchain (``qsub``/``sbatch``/``module``) is on PATH, which a plain
    non-login ``ssh host cmd`` misses. ``fleet exec`` is the on-host executor.
    """
    rest = " ".join(shlex.quote(str(arg)) for arg in args)  # fire may pass numeric args as int
    return f"cd {shlex.quote(root)} && chefe run fleet exec {rest}"
