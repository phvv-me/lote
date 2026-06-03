"""``qdel`` shell-out helper.

Wraps the ``qdel`` PBS command for parity with the rest of
:mod:`fleet.clients.pbs`. The implementation is deliberately
thin -- ``qdel`` has no structured output worth parsing, so we just run
the binary and surface its exit code via plumbum's ``ProcessExecutionError``.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from plumbum import local

from ...log import logger
from ..machine import Machine


def qdel(
    job_ids: str | Sequence[str],
    *,
    force: bool = False,
    machine: Machine = local,
    dry_run: bool = False,
) -> str:
    """Delete (or force-delete) one or more PBS jobs.

    job_ids: a single job id or an iterable of ids.
    force: pass ``-W force`` to ``qdel`` for a stronger termination.
    dry_run: when True, return the rendered command without running it.
    """
    ids = [job_ids] if isinstance(job_ids, str) else list(job_ids)
    command = ["qdel"]
    if force:
        command.extend(["-W", "force"])
    command.extend(ids)
    if dry_run:
        return shlex.join(command)
    logger.info("running {}", shlex.join(command))
    return str(machine[command[0]][command[1:]]())


__all__ = ["qdel"]
