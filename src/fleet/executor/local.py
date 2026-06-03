from __future__ import annotations

import os
from pathlib import Path


def get_job_local_root() -> Path:
    """Resolve the preferred job-local scratch directory.

    Prefers `LOCALDIR`, then `TMPDIR`, then `/tmp`.
    """

    for name in ("LOCALDIR", "TMPDIR"):
        if (value := os.getenv(name)) is not None:
            path = Path(value).expanduser()
            if path.exists():
                return path
    return Path("/tmp")


def ensure_job_local_root(*parts: str) -> Path:
    """Create and return a directory under the job-local root.

    parts: optional path components appended to the root.
    """

    path = get_job_local_root().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
