from pathlib import Path

import pathspec

from . import STATE_DIR

# Always skipped regardless of `.gitignore` — git internals and lote/tooling
# state a compute node never needs, which `.gitignore` may not list. `.git` carries
# no trailing slash so it matches both the superproject's `.git/` directory and the
# `.git` *file* every submodule carries; with a slash rsync would ship those files and
# fail trying to lay one over the submodule's `.git/` directory on the host.
ALWAYS_EXCLUDE = (".git", ".env", f"{STATE_DIR}/", ".pixi/", "__pycache__/")


class GitignoreFilter:
    """The repo's `.gitignore` turned into rsync excludes and a path matcher.

    Lote stops duplicating git's ignore list in `lote.toml`: everything git
    already ignores (envs, caches, build artifacts, model weights) is derived
    here, so `lote.toml`'s own `[sync].exclude` shrinks to just the
    committed-but-compute-irrelevant content (papers, datasets, figures) that
    `.gitignore` has no reason to list.

    root: the repo whose `.gitignore` drives the denylist; defaults to the
        current working directory, the repo you dispatch from.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path.cwd()
        lines = self.__lines(self.root / ".gitignore")
        # pyrefly: ignore  # pathspec's from_lines stub over-narrows to AnyStr
        self.spec = pathspec.GitIgnoreSpec.from_lines(lines)
        self.excludes = [*ALWAYS_EXCLUDE, *self.__rsync_patterns(lines)]

    def ignored(self, path: str | Path) -> bool:
        """Whether `path` (absolute or repo-relative) is git-ignored."""
        candidate = Path(path)
        if candidate.is_absolute() and candidate.is_relative_to(self.root):
            candidate = candidate.relative_to(self.root)
        return self.spec.match_file(candidate)

    @staticmethod
    def __lines(gitignore: Path) -> list[str]:
        return gitignore.read_text().splitlines() if gitignore.exists() else []

    @staticmethod
    def __rsync_patterns(lines: list[str]) -> list[str]:
        """Gitignore lines as rsync excludes — comments, blanks, and negations dropped.

        rsync excludes can't re-include, so `!`-negations are skipped rather than
        mistranslated; comment and blank lines carry no pattern.
        """
        stripped = (line.strip() for line in lines)
        return [line for line in stripped if line and not line.startswith(("#", "!"))]
