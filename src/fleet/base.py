"""Pydantic v2 base models for fleet.

Two bases cover everything fleet needs: a mutable value object and an
immutable config/record. `Field` is re-exported so call sites import it
from one place.
"""

from functools import cached_property

from pydantic import BaseModel, ConfigDict, Field

_IGNORED_TYPES: tuple[type, ...] = (cached_property,)

__all__ = ["Field", "FrozenModel", "Model"]


class Model(BaseModel):
    """Mutable model with standard types only."""

    model_config = ConfigDict(ignored_types=_IGNORED_TYPES)


class FrozenModel(BaseModel):
    """Immutable model with standard types only.

    Used for configuration objects and scheduler records that should never
    mutate after construction.
    """

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        ignored_types=_IGNORED_TYPES,
    )
