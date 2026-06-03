"""Centralized loguru configuration for fleet.

Usage:
    from .log import logger

Level controlled by the LOG_LEVEL env var (default: INFO).
"""

import os
import sys

from loguru import logger

logger.remove()

logger.add(
    sys.stderr,
    format="<level>{level.name[0]}</level>| <level>{message}</level>",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    colorize=True,
    diagnose=False,
)

__all__ = ["logger"]
