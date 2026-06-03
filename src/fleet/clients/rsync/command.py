from __future__ import annotations

from collections.abc import Sequence

from plumbum import local

from ...log import logger
from ...strflag import StrFlag


class Rsync(StrFlag):
    """rsync switches; OR-combine them and each member carries its literal flag."""

    ARCHIVE = "-a"  # recurse + preserve perms/times/symlinks/...
    COMPRESS = "-z"
    RELATIVE = "-R"  # recreate each source path under dest
    RECURSIVE = "-r"
    VERBOSE = "-v"
    DRY_RUN = "-n"
    CHECKSUM = "-c"  # compare by checksum, not size+mtime
    UPDATE = "-u"  # skip files newer on the receiver
    LINKS = "-l"
    PERMS = "-p"
    TIMES = "-t"
    HUMAN = "-h"
    DELETE = "--delete"  # mirror removals
    PARTIAL = "--partial"  # keep partially transferred files
    PROGRESS = "--progress"
    STATS = "--stats"


def rsync(
    sources: str | Sequence[str],
    dest: str,
    flags: Rsync | Sequence[Rsync] = Rsync.ARCHIVE | Rsync.COMPRESS,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    rsh: str | None = None,
    bwlimit: int | None = None,
    timeout: int | None = None,
    extra: Sequence[str] = (),
    run: bool = True,
) -> str:
    """Run ``rsync`` locally (it connects to remote hosts itself); return its stdout.

    With ``run=False`` return the rendered command string instead of executing.
    sources: one path or many. dest: ``host:/path/`` or a local dir.
    flags: combined :class:`Rsync` flag or sequence of members; single-letter ones
        merge into one ``-azR`` group.
    include / exclude: filter patterns, emitted in that order.
    rsh: remote shell (``-e``). bwlimit: KB/s cap. timeout: seconds.
    extra: raw flags for anything not covered above.
    """
    members = [*flags]
    short = "".join(member.string[1] for member in members if len(member.string) == 2)
    args: list[str] = [f"-{short}"] if short else []
    args += [member.string for member in members if member.string.startswith("--")]
    if rsh is not None:
        args += ["-e", rsh]
    if bwlimit is not None:
        args.append(f"--bwlimit={bwlimit}")
    if timeout is not None:
        args.append(f"--timeout={timeout}")
    for pattern in include:
        args += ["--include", pattern]
    for pattern in exclude:
        args += ["--exclude", pattern]
    args += [*extra, *([sources] if isinstance(sources, str) else sources), dest]
    command = local["rsync"][args]
    if not run:
        return str(command)
    logger.debug("$ {}", command)
    return str(command())
