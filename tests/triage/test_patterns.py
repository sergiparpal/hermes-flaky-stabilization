"""patterns: signature normalisation, upsert/lookup, FTS/LIKE, retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hermes_plugins.hermes_ci_triage import patterns


def _store(tmp_path, **kw):
    return patterns.PatternStore(tmp_path / "patterns.db", **kw)


def test_record_then_lookup_and_bump(tmp_path):
    s = _store(tmp_path)
    s.record("proj", "sig1", "broken_test", "AssertionError: x != y")
    got = s.lookup("proj", "sig1")
    assert got is not None
    assert got["category"] == "broken_test"
    assert got["occurrences"] == 1
    assert got["fuzzy"] is False

    s.record("proj", "sig1", "broken_test", "AssertionError: x != y")
    assert s.lookup("proj", "sig1")["occurrences"] == 2
    s.close()


def test_lookup_miss_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.lookup("proj", "absent") is None
    s.close()


def test_normalization_collapses_volatile_tokens():
    a = "2026-05-31T10:00:00Z ERROR at 0xdeadbeef line 42 /tmp/abc123/x.log"
    b = "2024-01-01T23:59:59Z ERROR at 0xfeedface line 99 /tmp/zzz999/y.log"
    assert patterns.compute_signature(a) == patterns.compute_signature(b)


def test_normalization_keeps_real_differences():
    a = patterns.compute_signature("ModuleNotFoundError: No module named 'foo'")
    b = patterns.compute_signature("AssertionError: expected true")
    assert a != b


def test_retention_prunes_stale_rows(tmp_path):
    s = _store(tmp_path, retention_days=180)
    old = datetime.now(UTC) - timedelta(days=200)
    s.record("proj", "old", "infra", "stale sample", now=old)
    s.record("proj", "fresh", "infra", "fresh sample")  # real 'now'
    s.prune("proj")
    assert s.lookup("proj", "old") is None
    assert s.lookup("proj", "fresh") is not None
    s.close()


def test_per_project_row_cap_evicts_oldest(tmp_path):
    s = _store(tmp_path, max_rows_per_project=5)
    base = datetime.now(UTC) - timedelta(days=1)
    for i in range(20):
        s.record("proj", f"sig{i}", "infra", f"sample {i}",
                 now=base + timedelta(seconds=i))
    assert s.count("proj") <= 5
    # newest survive, oldest evicted
    assert s.lookup("proj", "sig19") is not None
    assert s.lookup("proj", "sig0") is None
    s.close()


def test_fts_forced_off_uses_like_fuzzy(tmp_path):
    s = _store(tmp_path, fts=False)
    assert s.fts is False
    s.record("proj", "sig", "data",
             "JSONDecodeError unexpected token in fixture data")
    assert s.lookup("proj", "sig") is not None  # exact
    fuzzy = s.lookup("proj", "other-sig", "JSONDecodeError unexpected token")
    assert fuzzy is not None and fuzzy["fuzzy"] is True
    s.close()


def test_fts_when_available(tmp_path):
    if not patterns.fts5_available():
        pytest.skip("FTS5 not available in this sqlite build")
    s = _store(tmp_path, fts=True)
    assert s.fts is True
    s.record("proj", "sig", "infra", "connection refused to redis at startup")
    fuzzy = s.lookup("proj", "other-sig", "connection refused redis")
    assert fuzzy is not None and fuzzy["fuzzy"] is True
    s.close()


def _fts_variants(fts):
    if fts and not patterns.fts5_available():
        pytest.skip("FTS5 not available in this sqlite build")


@pytest.mark.parametrize("fts", [False, True])
def test_fuzzy_lookup_shared_stopword_returns_nothing(tmp_path, fts):
    """Regression: one ubiquitous shared token ("error", "failed") must not
    surface the project's most-seen pattern as a 'similar' prior for an
    unrelated failure."""
    _fts_variants(fts)
    s = _store(tmp_path, fts=fts)
    for _ in range(5):  # the project's most-seen pattern
        s.record("proj", "hot-sig", "infra",
                 "ERROR: connection refused to redis at startup")
    # Shares only the ubiquitous "error" token with the stored sample.
    assert s.lookup("proj", "other-sig",
                    "ERROR: widget frobnicator melted unexpectedly") is None
    # A query of nothing but failure vocabulary has no fuzzy signal at all.
    assert s.lookup("proj", "other-sig", "error failure exception failed") is None
    s.close()


@pytest.mark.parametrize("fts", [False, True])
def test_fuzzy_lookup_single_shared_token_is_not_similarity(tmp_path, fts):
    _fts_variants(fts)
    s = _store(tmp_path, fts=fts)
    s.record("proj", "sig", "infra", "connection refused to redis at startup")
    # Only "redis" is shared; one non-stopword token is not meaningful overlap.
    assert s.lookup("proj", "other-sig", "redis wombat grapefruit") is None
    # Two shared non-stopword tokens are.
    fuzzy = s.lookup("proj", "other-sig", "redis startup wombat")
    assert fuzzy is not None and fuzzy["fuzzy"] is True
    s.close()
