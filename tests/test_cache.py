from pathlib import Path

import pytest

from lote.cache import Cache, RunRecord
from lote.history import History
from lote.models import Target


def make_run(
    handle: str, *, submitted_at: str, script: str = "a.sh", target: str = "dgx"
) -> RunRecord:
    """A fully-populated :class:`RunRecord` for cache tests (only varied fields are args)."""
    return RunRecord(
        handle=handle,
        target=target,
        kind="ssh",
        script=script,
        args="",
        git_sha="abc1234",
        dirty=0,
        submitted_at=submitted_at,
    )


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


def test_cache_records_and_orders_runs(workdir: Path) -> None:
    """recent() returns newest-first by submitted_at and record upserts by handle."""
    cache = Cache(workdir / "db.sqlite")
    cache.record(make_run("1", submitted_at="2024-01-01T00:00:00", script="a"))
    cache.record(make_run("2", submitted_at="2024-01-02T00:00:00", script="b"))
    cache.record(make_run("1", submitted_at="2024-01-03T00:00:00", script="a2"))  # upsert
    recent = cache.recent()
    assert [r.handle for r in recent] == ["1", "2"]  # handle 1 re-dated to newest
    assert recent[0].script == "a2"


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
