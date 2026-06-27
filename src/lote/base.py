"""Pydantic v2 base models for lote.

The house bases live in :mod:`patos`; lote re-exports the two it needs (a mutable value object
and an immutable config/record) plus `Field`, so call sites import everything from one place.
"""

from patos import FrozenModel, Model
from pydantic import Field

__all__ = ["Field", "FrozenModel", "Model"]
