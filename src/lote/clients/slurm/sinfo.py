def build_sinfo_command() -> list[str]:
    """Build a ``sinfo`` command listing one partition name per line."""
    return ["sinfo", "--noheader", "--format=%P"]


def parse_sinfo_output(output: str) -> list[str]:
    """Partition names from :func:`build_sinfo_command` output.

    The default partition's trailing ``*`` marker is stripped and duplicates
    (one row per node state) are dropped, preserving first-seen order.
    """
    partitions: list[str] = []
    for line in output.splitlines():
        name = line.strip().removesuffix("*")
        if name and name not in partitions:
            partitions.append(name)
    return partitions
