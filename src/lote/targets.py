"""Targets from ``~/.ssh/config`` and the over-ssh probe that describes each host."""

from pathlib import Path

from plumbum import SshMachine

from .models import LOGIN, Config, NodeClass, Target

# The user's ssh client config; its concrete ``Host`` aliases are lote's targets.
SSH_CONFIG = Path.home() / ".ssh" / "config"

# Pre-sync, the one thing we need before rsyncing is *where* to put the repo: an
# HPC ``/work`` area if there is one (home dirs there are tiny), else ``~/projects``.
ROOT_FINDER = (
    'w=$(ls -d /work/*/"$USER"/projects 2>/dev/null | head -1); echo "${w:-$HOME/projects}"'
)

# A capability probe that needs nothing installed on the host: with stock tools
# it reads the repo root, scheduler, GPU, system memory, group, and interactive
# PBS queue, and prints them as ``key=value`` lines. Run in a login shell so
# ``/etc/profile.d`` puts the HPC scheduler on PATH; this lets ``lote probe``
# preview a host before any sync or install, and onboarding reuse the same read.
CAPABILITIES = "\n".join(
    (
        f"root=$({ROOT_FINDER})",
        "if command -v sbatch >/dev/null 2>&1; then kind=slurm;"
        " elif command -v qsub >/dev/null 2>&1; then kind=pbs; else kind=ssh; fi",
        "gpu=$(nvidia-smi --query-gpu=name,memory.total"
        " --format=csv,noheader,nounits 2>/dev/null | head -1)",
        r"mem=$(sed -n 's/^MemTotal:[[:space:]]*\([0-9]*\).*/\1/p' /proc/meminfo 2>/dev/null)",
        "queue=$(qstat -Q 2>/dev/null | awk 'NR>2 && tolower($1) ~ /interact/ {print $1; exit}')",
        # Miyabi's qstat wrapper rejects ``-Q``, so fall back to its ``--rsc`` tree and
        # take the top-level interactive router (``interact-g``), which is the queue
        # ``qsub -I`` accepts; the indented ``_n1`` leaf is access-denied, and ``mig`` is
        # the fractional-GPU pool we never want for a full interactive node.
        '[ -z "$queue" ] && queue=$(qstat --rsc 2>/dev/null'
        " | awk '/^interact/ && tolower($1) !~ /mig/ {print $1; exit}')",
        "printf 'root=%s\\nkind=%s\\ngpu=%s\\nmem=%s\\naccount=%s\\nqueue=%s\\n'"
        ' "$root" "$kind" "$gpu" "$mem" "$(id -gn)" "$queue"',
    )
)


def ssh_hosts(config_path: Path = SSH_CONFIG) -> list[str]:
    """Concrete ``Host`` aliases from ``~/.ssh/config``, in file order.

    Splits each line on any whitespace (ssh accepts tabs as well as spaces after
    ``Host``), splits multi-alias ``Host a b`` lines, and drops any pattern token
    (one containing ``*`` or ``?``, e.g. ``Host *`` or ``dl*``), so the result is
    the list of real, connectable destinations lote can target. Non-``Host``
    directives (``Include``, ``HostName``, ...) are skipped.

    config_path: path to the ssh client config to parse.
    """
    if not config_path.exists():
        return []
    hosts: list[str] = []
    for line in config_path.read_text().splitlines():
        keyword, *aliases = line.split() or [""]
        if keyword.lower() != "host":
            continue
        concrete = (a for a in aliases if "*" not in a and "?" not in a)
        hosts.extend(alias for alias in concrete if alias not in hosts)
    return hosts


def find_root(remote: SshMachine) -> str:
    """The repo root to use on the host (an HPC ``/work`` area, else ``~/projects``)."""
    return str(remote["bash"]["-lc", ROOT_FINDER]().strip())


def probe_capabilities(remote: SshMachine, alias: str) -> Target:
    """Probe ``alias`` over ssh without syncing or installing, as a :class:`Target`.

    Runs the stock-tool :data:`CAPABILITIES` script in a login shell and parses
    its ``key=value`` lines, so it needs nothing on the host — the same Target
    onboarding caches, available before a single byte is shipped. What it sees
    is the node it landed on, so the capabilities go under the ``login`` class;
    queue classes come later from submitted probe jobs (see :mod:`lote.nodes`).
    """
    raw = remote["bash"]["-lc", CAPABILITIES]()
    fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    name, _, vram = fields["gpu"].partition(",")
    sysmem_kb = fields["mem"]
    login = NodeClass(
        name=LOGIN,
        gpu_name=name.strip() or None,
        gpu_count=1 if name.strip() else 0,
        gpu_mem_mb=int(vram) if vram.strip().isdigit() else None,
        sysmem_gb=round(int(sysmem_kb) / 1024**2) if sysmem_kb.isdigit() else None,
    )
    return Target(
        name=alias,
        root=fields["root"],
        kind=fields["kind"],
        account=fields["account"] or None,
        queue=fields["queue"] or None,
        classes={LOGIN: login},
    )


def resolve(alias: str, config: Config, facts: Target) -> Target:
    """Build a :class:`Target` from probe ``facts``, applying any ``[hints]`` override.

    The probe already emits every field, so a ``[hints.<alias>]`` entry is just a
    power-user override (its keys are :class:`Target` field names). A key that is
    not a Target field is a config typo and fails loudly here, instead of being
    silently swallowed and "applying" nothing.
    """
    hints = config.hints.get(alias, {})
    unknown = set(hints) - set(Target.model_fields)
    if unknown:
        raise LookupError(
            f"unknown key(s) {', '.join(sorted(unknown))} in [hints.{alias}]; "
            f"valid keys are {', '.join(Target.model_fields)}"
        )
    return Target.model_validate({**facts.model_dump(), **hints})


def smallest_fit(targets: list[Target], needs_gb: float) -> Target:
    """Smallest-VRAM target that still satisfies ``needs_gb`` (keeps big iron free).

    targets: candidate resolved targets.
    needs_gb: requested memory in GB.
    """
    fitting = sorted((t for t in targets if t.fits(needs_gb)), key=lambda t: t.vram_gb or 0.0)
    if not fitting:
        have = ", ".join(f"{t.name}={t.vram_gb}" for t in targets)
        raise LookupError(f"no target fits {needs_gb} GB; have: {have}")
    return fitting[0]
