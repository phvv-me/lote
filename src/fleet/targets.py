"""Targets from ``~/.ssh/config`` and the in-env probe that describes each host."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from plumbum import SshMachine

from .models import Config, Target

# The user's ssh client config; its concrete ``Host`` aliases are fleet's targets.
SSH_CONFIG = Path.home() / ".ssh" / "config"

# Pre-sync, the one thing we need before rsyncing is *where* to put the repo: an
# HPC ``/work`` area if there is one (home dirs there are tiny), else ``~/projects``.
ROOT_FINDER = (
    'w=$(ls -d /work/*/"$USER"/projects 2>/dev/null | head -1); echo "${w:-$HOME/projects}"'
)


def ssh_hosts(config_path: Path = SSH_CONFIG) -> list[str]:
    """Concrete ``Host`` aliases from ``~/.ssh/config``, in file order.

    Splits multi-alias ``Host a b`` lines and drops any pattern token (one
    containing ``*`` or ``?``, e.g. ``Host *`` or ``dl*``), so the result is the
    list of real, connectable destinations fleet can target.

    config_path: path to the ssh client config to parse.
    """
    if not config_path.exists():
        return []
    hosts: list[str] = []
    for line in config_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("host "):
            continue
        aliases = (a for a in stripped.split()[1:] if "*" not in a and "?" not in a)
        hosts.extend(alias for alias in aliases if alias not in hosts)
    return hosts


def find_root(remote: SshMachine) -> str:
    """The repo root to use on the host (an HPC ``/work`` area, else ``~/projects``)."""
    return str(remote["bash"]["-lc", ROOT_FINDER]().strip())


def probe_host(remote: SshMachine, alias: str, root: str) -> dict[str, Any]:
    """Run the in-env probe on the synced host and return the host as a Target dict.

    Runs in a LOGIN shell so ``/etc/profile.d`` puts the HPC toolchain on PATH
    (``shutil.which("qsub")`` then finds the cluster's qsub), and via ``chefe run``
    so the probe shares the fleet models + ``psutil`` from the installed env.
    """
    probe = f"chefe run python -m fleet.probe {shlex.quote(alias)} {shlex.quote(root)}"
    command = f"cd {shlex.quote(root)} && {probe}"
    result: dict[str, Any] = json.loads(remote["bash"]["-lc", command]())
    return result


def resolve(alias: str, config: Config, facts: dict[str, Any]) -> Target:
    """Build a :class:`Target` from probe ``facts`` (a Target dict), applying ``[hints]``.

    The probe already emits every field, so a ``[hints.<alias>]`` entry is just a
    power-user override (its keys are :class:`Target` field names).
    """
    return Target.model_validate({**facts, **config.hints.get(alias, {})})


def smallest_fit(targets: list[Target], needs_gb: float) -> Target:
    """Smallest-VRAM target that still satisfies ``needs_gb`` (keeps big iron free).

    targets: candidate resolved targets.
    needs_gb: requested memory in GB.
    """
    fitting = sorted((t for t in targets if t.fits(needs_gb)), key=lambda t: t.vram_gb or 0.0)
    if not fitting:
        have = ", ".join(f"{t.name}={t.vram_gb}" for t in targets)
        raise SystemExit(f"no target fits {needs_gb} GB; have: {have}")
    return fitting[0]
