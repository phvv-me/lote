from collections.abc import Sequence

from patos import StrFlag
from plumbum import CommandNotFound, local

from ...log import logger

# macOS ships Apple's openrsync as /usr/bin/rsync; upstream rsync usually arrives
# via Homebrew or MacPorts at these roots, searched after PATH.
UPSTREAM_FALLBACKS = ("/opt/homebrew/bin/rsync", "/usr/local/bin/rsync", "/opt/local/bin/rsync")


def binary(*, mirror: bool) -> str:
    """The local rsync to run, preferring upstream rsync over Apple's openrsync.

    openrsync cannot prune inside directories that exist on both ends of a remote
    `--relative` transfer, so a remote mirror silently keeps the stale files it
    was meant to remove. PATH is searched first, then the Homebrew/MacPorts roots.
    mirror: whether the transfer prunes a remote end (`--delete`), which demands
        upstream rsync and raises when only openrsync is installed.
    """
    for candidate in ("rsync", *UPSTREAM_FALLBACKS):
        try:
            if "openrsync" not in local[candidate]("--version"):
                return candidate
        except CommandNotFound, OSError:
            continue
    if mirror:
        raise RuntimeError(
            "mirroring needs upstream rsync, but only Apple's openrsync is installed "
            "and it cannot prune stale files on a remote host. Run `brew install rsync`"
        )
    return "rsync"


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
    protect: Sequence[str] = (),
    rsh: str | None = None,
    bwlimit: int | None = None,
    timeout: int | None = None,
    extra: Sequence[str] = (),
    run: bool = True,
) -> str:
    """Run ``rsync`` locally (it connects to remote hosts itself); return its stdout.

    The binary comes from :func:`binary`, which prefers upstream rsync and refuses
    to mirror a remote end with Apple's openrsync.
    With ``run=False`` return the rendered command string instead of executing.
    sources: one path or many. dest: ``host:/path/`` or a local dir.
    flags: combined :class:`Rsync` flag or sequence of members; single-letter ones
        merge into one ``-azR`` group.
    include / exclude: filter patterns, emitted in that order.
    protect: receiver-side ``protect`` filter rules emitted before include/exclude,
        shielding remote-only paths from ``--delete`` pruning.
    rsh: remote shell (``-e``). bwlimit: KB/s cap. timeout: seconds.
    extra: raw flags for anything not covered above.
    """
    members = [*flags]
    paths = [*([sources] if isinstance(sources, str) else sources), dest]
    mirror = Rsync.DELETE in members and any(":" in path for path in paths)
    short = "".join(member.string[1] for member in members if len(member.string) == 2)
    args: list[str] = [f"-{short}"] if short else []
    args += [member.string for member in members if member.string.startswith("--")]
    if rsh is not None:
        args += ["-e", rsh]
    if bwlimit is not None:
        args.append(f"--bwlimit={bwlimit}")
    if timeout is not None:
        args.append(f"--timeout={timeout}")
    for pattern in protect:
        args += ["--filter", f"protect {pattern}"]
    for pattern in include:
        args += ["--include", pattern]
    for pattern in exclude:
        args += ["--exclude", pattern]
    args += [*extra, *paths]
    command = local[binary(mirror=mirror)][args]
    if not run:
        return str(command)
    logger.debug("$ {}", command)
    return str(command())
