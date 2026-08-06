import fcntl
import hashlib
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self, TextIO

import pathspec

from . import STATE_DIR

# Always skipped regardless of `.gitignore` — git internals and lote/tooling
# state a compute node never needs, which `.gitignore` may not list. `.git` carries
# no trailing slash so it matches both the superproject's `.git/` directory and the
# `.git` *file* every submodule carries; with a slash rsync would ship those files and
# fail trying to lay one over the submodule's `.git/` directory on the host.
ALWAYS_EXCLUDE = (".git", ".env", f"{STATE_DIR}/", ".pixi/", "__pycache__/")


class SyncLock:
    """Serialize destructive mirrors to one target across local lote processes.

    target: SSH alias whose remote tree is being mirrored.
    root: local repository root that owns the `.lote` state directory.
    """

    def __init__(self, target: str, root: Path | None = None) -> None:
        digest = hashlib.blake2s(target.encode(), digest_size=8).hexdigest()
        self.path = (root or Path.cwd()) / STATE_DIR / "locks" / f"sync-{digest}.lock"
        self.file: TextIO | None = None

    def __enter__(self) -> Self:
        """Wait for this target's mirror lock and hold it until context exit."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+")
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        except OSError:
            file.close()
            raise
        self.file = file
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the kernel lock even when rsync raises."""
        del exc_type, exc_value, traceback
        assert self.file is not None
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


class GitignoreFilter:
    """Git ignore rules for rsync and the local change watcher.

    Rsync reads the repository root ignore file once as a global rule set, then
    discovers every nested `.gitignore` as it descends. This preserves each
    file's directory context and lets rsync apply the same excludes while
    pruning the receiver. The local watcher uses `pathspec` for the root rules.

    root: the repo whose `.gitignore` drives the denylist; defaults to the
        current working directory, the repo you dispatch from.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path.cwd()
        gitignore = self.root / ".gitignore"
        lines = self.__lines(gitignore)
        # pyrefly: ignore  # pathspec's from_lines stub over-narrows to AnyStr
        self.spec = pathspec.GitIgnoreSpec.from_lines(lines)
        self.filters = [
            *(["merge,- .gitignore"] if gitignore.is_file() else []),
            ":- .gitignore",
        ]
        self.excludes = list(ALWAYS_EXCLUDE)

    def ignored(self, path: str | Path) -> bool:
        """Whether `path` (absolute or repo-relative) is git-ignored."""
        candidate = Path(path)
        if candidate.is_absolute() and candidate.is_relative_to(self.root):
            candidate = candidate.relative_to(self.root)
        return self.spec.match_file(candidate)

    def control_files(self, sources: Sequence[str]) -> list[str]:
        """Ignore files above source roots that the receiver needs before deletion.

        A narrow source such as `research/projects` inherits `research/.gitignore`
        on the sender, but that parent file is outside the transferred subtree.
        Shipping each existing ancestor from the repository root downward lets
        `--delete-after` apply the same directory-relative rules on the receiver.
        """
        files: list[str] = []
        seen: set[Path] = set()
        for source in sources:
            path = Path(source)
            if path.is_absolute():
                path = path.relative_to(self.root)
            ancestors = reversed((path.parent, *path.parent.parents))
            for ancestor in ancestors:
                gitignore = ancestor / ".gitignore"
                if gitignore in seen or not (self.root / gitignore).is_file():
                    continue
                seen.add(gitignore)
                files.append(str(gitignore))
        return files

    @staticmethod
    def __lines(gitignore: Path) -> list[str]:
        return gitignore.read_text().splitlines() if gitignore.exists() else []
