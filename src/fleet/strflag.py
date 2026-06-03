"""A `Flag` whose members carry their literal CLI string."""

from __future__ import annotations

from enum import Flag

__all__ = ["StrFlag"]


class StrFlag(Flag):
    """`Flag` whose members are declared as their literal string value.

    Each member is assigned a power-of-two `_value_` so members stay OR-combinable
    and iterable, while the declared string is kept on the `.string` attribute.

    ```python
    class Opt(StrFlag):
        ALL = "-a"
        BIG = "--big"

    [m.string for m in (Opt.ALL | Opt.BIG)]  # ['-a', '--big']
    ```
    """

    string: str

    def __new__(cls, string: str) -> StrFlag:
        member = object.__new__(cls)
        member._value_ = 1 << len(cls.__members__)
        member.string = string
        return member
