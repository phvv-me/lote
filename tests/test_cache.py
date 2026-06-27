from pathlib import Path

import pytest

from lote.cache import Cache
from lote.history import History
from lote.models import LOGIN, NodeClass, Target

from .conftest import make_run


def test_cache_facts_roundtrip(workdir: Path) -> None:
    """save_facts then facts returns an equal Target; an unknown alias is None."""
    cache = Cache(workdir / "db.sqlite")
    target = Target(name="dgx", kind="ssh", root="/work")
    cache.save_facts("dgx", target)
    assert cache.facts("dgx") == target
    assert cache.facts("other") is None


def test_cache_save_facts_upserts_by_alias(workdir: Path) -> None:
    """Re-saving an alias overwrites rather than duplicating."""
    cache = Cache(workdir / "db.sqlite")
    cache.save_facts("dgx", Target(name="dgx", root="/a"))
    cache.save_facts("dgx", Target(name="dgx", root="/b"))
    assert cache.facts("dgx") == Target(name="dgx", root="/b")
    assert cache.db.execute("SELECT COUNT(*) FROM hosts").fetchone()[0] == 1


def test_cache_facts_roundtrips_node_classes(workdir: Path) -> None:
    """A target's classes land one row per class key and reassemble on read."""
    cache = Cache(workdir / "db.sqlite")
    target = Target(
        name="hpc",
        kind="pbs",
        classes={
            LOGIN: NodeClass(name=LOGIN, sysmem_gb=128),
            "debug-g": NodeClass(name="debug-g", gpu_name="NVIDIA H100", gpu_mem_mb=96 * 1024),
        },
    )
    cache.save_facts("hpc", target)
    assert cache.facts("hpc") == target
    assert cache.db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2


def test_cache_save_node_upserts_one_class(workdir: Path) -> None:
    """save_node adds or refreshes a single class without touching its siblings."""
    cache = Cache(workdir / "db.sqlite")
    cache.save_facts("hpc", Target(name="hpc", classes={LOGIN: NodeClass(name=LOGIN)}))
    cache.save_node("hpc", NodeClass(name="prepost", sysmem_gb=512))
    cache.save_node("hpc", NodeClass(name="prepost", sysmem_gb=1024))  # upsert
    classes = cache.classes("hpc")
    assert set(classes) == {LOGIN, "prepost"}
    assert classes["prepost"].sysmem_gb == 1024


def test_cache_save_facts_replaces_stale_classes(workdir: Path) -> None:
    """A full save drops class rows for queues the cluster no longer has."""
    cache = Cache(workdir / "db.sqlite")
    cache.save_facts("hpc", Target(name="hpc", classes={"old-q": NodeClass(name="old-q")}))
    cache.save_facts("hpc", Target(name="hpc", classes={"new-q": NodeClass(name="new-q")}))
    assert set(cache.classes("hpc")) == {"new-q"}


def test_cache_records_and_orders_runs(workdir: Path) -> None:
    """recent() returns newest-first by submitted_at and record upserts by handle."""
    cache = Cache(workdir / "db.sqlite")
    cache.record(make_run("1", submitted_at="2024-01-01T00:00:00", script="a"))
    cache.record(make_run("2", submitted_at="2024-01-02T00:00:00", script="b"))
    cache.record(make_run("1", submitted_at="2024-01-03T00:00:00", script="a2"))  # upsert
    recent = cache.recent()
    assert [r.handle for r in recent] == ["1", "2"]  # handle 1 re-dated to newest
    assert recent[0].script == "a2"


def test_cache_resolve_memoizes_verdict_without_losing_provenance(workdir: Path) -> None:
    """resolve writes the live verdict back onto the run (so status reads it without re-probing)
    while preserving the run's provenance and submit time, and never duplicates the row."""
    cache = Cache(workdir / "db.sqlite")
    cache.record(make_run("7", submitted_at="2024-01-01T00:00:00", script="a.sh"))
    cache.resolve(cache.run("7"), state="F", exit_code=0, verdict="ok")
    resolved = cache.run("7")
    assert (resolved.verdict, resolved.exit_code, resolved.state) == ("ok", 0, "F")
    assert resolved.script == "a.sh" and resolved.submitted_at == "2024-01-01T00:00:00"
    assert cache.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_cache_recent_limit(workdir: Path) -> None:
    """recent(limit) caps the result count."""
    cache = Cache(workdir / "db.sqlite")
    for i in range(5):
        cache.record(make_run(str(i), submitted_at=f"2024-01-0{i + 1}T00:00:00"))
    assert len(cache.recent(limit=2)) == 2


def test_cache_run_missing_raises(workdir: Path) -> None:
    """run(handle) for an unknown handle is a LookupError (the CLI boundary turns it
    into a clean one-line exit)."""
    cache = Cache(workdir / "db.sqlite")
    with pytest.raises(LookupError, match="no recorded run"):
        cache.run("nope")


def test_cache_run_returns_recorded(workdir: Path) -> None:
    """run(handle) returns the recorded run for a known handle."""
    cache = Cache(workdir / "db.sqlite")
    cache.record(make_run("42", submitted_at="2024-01-01T00:00:00", target="dgx"))
    assert cache.run("42").target == "dgx"


def test_history_records_and_disables(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """record appends a timed event picking the first str arg as target; recent reads them back."""
    history = History(workdir / "db.sqlite")
    history.record("submit", ("dgx", "a.sh"), 0.0, "ok", handle="42")
    [event] = history.recent()
    assert event.command == "submit" and event.target == "dgx" and event.handle == "42"
    assert event.duration_ms is not None and event.duration_ms >= 0


def test_history_opt_out(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LOTE_NO_HISTORY=1 makes record a no-op."""
    monkeypatch.setenv("LOTE_NO_HISTORY", "1")
    history = History(workdir / "db.sqlite")
    history.record("ls", (), 0.0, "ok")
    assert history.recent() == []
