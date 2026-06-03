from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from ..base import FrozenModel

# Repo-root ``fleet.toml``; this file is ``<root>/common/fleet/models/config.py``.
CONFIG_FILE = Path(__file__).resolve().parents[3] / "fleet.toml"


class Sync(FrozenModel):
    """The rsync allowlist / denylist from the ``[sync]`` table.

    include: paths a compute node needs (``rsync -R`` preserves them).
    exclude: heavy / regenerable patterns to skip within the included paths.
    """

    include: list[str] = []
    exclude: list[str] = []


class Config(BaseSettings):
    """Parsed ``fleet.toml``: the rsync scope, plus optional target/hint overrides.

    Targets default to the concrete ``Host`` aliases in ``~/.ssh/config`` and
    each target's capabilities (scheduler, repo root, GPU) are auto-discovered by
    the ssh probe — so ``targets`` and ``hints`` are empty by default and exist
    only as power-user overrides. ``fleet.toml`` is essentially just ``[sync]``.
    """

    model_config = SettingsConfigDict(toml_file=CONFIG_FILE, frozen=True)

    sync: Sync = Sync()
    targets: list[str] = []
    hints: dict[str, dict[str, Any]] = {}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (TomlConfigSettingsSource(settings_cls),)
