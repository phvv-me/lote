from __future__ import annotations

from pathlib import Path

import pathspec

# The repo root: this file is `<root>/common/fleet/sync.py`.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Always skipped regardless of `.gitignore` — git internals and fleet/tooling
# state a compute node never needs, which `.gitignore` may not list.
ALWAYS_EXCLUDE = (".git/", ".fleet/", ".pixi/", "__pycache__/")


class GitignoreFilter:
    """The repo's `.gitignore` turned into rsync excludes and a path matcher.

    Fleet stops duplicating git's ignore list in `fleet.toml`: everything git
    already ignores (envs, caches, build artifacts, model weights) is derived
    here, so `fleet.toml`'s own `[sync].exclude` shrinks to just the
    committed-but-compute-irrelevant content (papers, datasets, figures) that
    `.gitignore` has no reason to list.

    root: the repo whose `.gitignore` drives the denylist.
    """

    def __init__(self, root: Path = REPO_ROOT) -> None:
        self.root = root
        lines = self.__lines(root / ".gitignore")
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
