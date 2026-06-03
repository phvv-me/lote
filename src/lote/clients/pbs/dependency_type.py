from __future__ import annotations

from enum import StrEnum


class DependencyType(StrEnum):
    """Common PBS dependency kinds."""

    AFTER = "after"
    AFTER_ANY = "afterany"
    AFTER_OK = "afterok"
    BEFORE = "before"
    BEFORE_ANY = "beforeany"
    BEFORE_OK = "beforeok"
