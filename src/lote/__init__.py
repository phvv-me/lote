# The package name *is* the project name (the build maps pyproject -> this package),
# so a rename is a single move of the `src/lote` folder. Everything naming derives
# from it; reading pyproject.toml at runtime would be fragile (it isn't in the wheel).
NAME = __name__

# The config a user writes, and the state directory generated beside it.
CONFIG = f"{NAME}.toml"
STATE_DIR = f".{NAME}"
