from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from .. import CONFIG, STATE_DIR
from ..base import FrozenModel

# Config file read from the current working directory — the repo you dispatch
# from. Relative, so ``TomlConfigSettingsSource`` resolves it against the cwd at
# call time (run ``lote`` from your repo root, next to its ``lote.toml``).
CONFIG_FILE = CONFIG

# A ``[hints.<alias>]`` table overrides :class:`Target` fields; their values are
# the scalar TOML types those fields take (root/kind strings, GPU/mem ints, the
# login-shell bool), never nested tables.
type Hint = dict[str, str | int | bool]


class Sync(FrozenModel):
    """The rsync allowlist / denylist / keep-list from the ``[sync]`` table.

    Sync mirrors the local tree onto the host (``--delete``), so a renamed or
    removed local path can never linger remotely and shadow its replacement.
    ``protect`` is what makes that mirroring safe for paths that legitimately
    exist only on the host.

    include: paths a compute node needs (``rsync -R`` preserves them).
    exclude: heavy / regenerable patterns to skip within the included paths.
    protect: rsync filter patterns pruning must never delete on the host. The
        defaults shield experiment results, log trees and files, and lote's own
        job state (the ``dir/***`` form covers the directory and everything in it).
    """

    include: list[str] = []
    exclude: list[str] = []
    protect: list[str] = [f"{STATE_DIR}/***", "results/***", "logs/***", "*.log"]


class Config(BaseSettings):
    """Parsed ``lote.toml``: the rsync scope, plus optional target/hint overrides.

    Targets default to the concrete ``Host`` aliases in ``~/.ssh/config`` and
    each target's capabilities (scheduler, repo root, GPU) are auto-discovered by
    the ssh probe — so ``targets`` and ``hints`` are empty by default and exist
    only as power-user overrides. ``lote.toml`` is essentially just ``[sync]``.
    """

    model_config = SettingsConfigDict(toml_file=CONFIG_FILE, frozen=True)

    sync: Sync = Sync()
    targets: list[str] = []
    hints: dict[str, Hint] = {}

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
