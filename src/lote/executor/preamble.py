"""A job wrapper that guarantees activation, so a submitted script can never
silently fail in ``$HOME`` with the wrong interpreter.

The #1 HPC footgun: a batch job starts in ``$HOME`` (not the submit dir) in a
shell with none of the user env on PATH, so a script using repo-relative paths or
bare ``python`` dies instantly and "succeeds" with empty output. The executor
already passes every scheduler directive as a ``qsub``/``sbatch`` flag, so the
wrapper carries no directives -- it just ``cd``s into the submit directory and
puts the user install dirs on PATH, then ``exec``s the user script. Idempotent: a
script that already cds / activates re-does it harmlessly; one that forgot is
rescued.
"""

import shlex
from typing import TYPE_CHECKING

from ..environment import USER_BINS

if TYPE_CHECKING:
    from pathlib import Path


def write_wrapper(
    script: Path,
    logs_dir: Path,
    *,
    workdir_var: str,
    dry_run: bool = False,
) -> Path:
    """Write and return a wrapper that cds into ``$<workdir_var>``, sets PATH, execs ``script``.

    workdir_var: the scheduler's submit-dir env var (``PBS_O_WORKDIR`` /
        ``SLURM_SUBMIT_DIR``). dry_run skips the write but still returns the path.
    """
    path_export = ":".join(USER_BINS)
    wrapper = logs_dir / f"{script.stem}.wrapper.sh"
    if not dry_run:
        wrapper.write_text(
            "#!/bin/bash\n"
            f'cd "${{{workdir_var}:-$HOME}}"\n'
            f"export PATH={path_export}:$PATH\n"
            f"exec bash {shlex.quote(str(script.resolve()))}\n"
        )
    return wrapper
