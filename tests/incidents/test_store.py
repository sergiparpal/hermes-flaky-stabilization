"""SQLite + FTS5 store tests (plan §2). Self-contained, no network."""

import os
import sqlite3

import pytest
from jira_incidents.store import IncidentStore


def _inc(key, summary, status="Open", root_cause="", body=""):
    return {"key": key, "summary": summary, "status": status,
            "root_cause": root_cause, "body": body}


@pytest.fixture
def store(tmp_home):
    db = os.path.join(tmp_home, "idx", "hermes-jira-incidents.db")
    s = IncidentStore(db)
    yield s
    s.close()


class TestProfileScoping:
    def test_db_lands_under_hermes_home(self, store, tmp_home):
        assert store.db_path.startswith(tmp_home)
        assert os.path.exists(store.db_path)


class TestUpsertAndGet:
    def test_get_by_key(self, store):
        store.upsert(_inc("INC-1", "disk full on db host", root_cause="log rotation off"))
        row = store.get("INC-1")
        assert row["key"] == "INC-1"
        assert row["summary"] == "disk full on db host"
        assert row["root_cause"] == "log rotation off"

    def test_get_missing_returns_none(self, store):
        assert store.get("NOPE-9") is None

    def test_get_blank_returns_none(self, store):
        assert store.get("") is None
        assert store.get("   ") is None

    def test_upsert_replaces(self, store):
        store.upsert(_inc("INC-2", "first"))
        store.upsert(_inc("INC-2", "second"))
        assert store.get("INC-2")["summary"] == "second"
        assert store.count() == 1

    def test_upsert_requires_key(self, store):
        with pytest.raises(ValueError):
            store.upsert({"summary": "no key"})


class TestSearch:
    def test_returns_expected_keys(self, store):
        store.upsert(_inc("INC-1", "database connection pool exhausted"))
        store.upsert(_inc("INC-2", "payment gateway timeout"))
        store.upsert(_inc("INC-3", "database replica lag spike"))
        keys = {r["key"] for r in store.search("database", limit=10)}
        assert keys == {"INC-1", "INC-3"}

    def test_ranking_prefers_more_matches(self, store):
        store.upsert(_inc("INC-1", "cache cache cache eviction storm", body="cache"))
        store.upsert(_inc("INC-2", "unrelated networking blip", body="cache mentioned once"))
        results = store.search("cache", limit=5)
        assert results[0]["key"] == "INC-1"

    def test_matches_body_and_root_cause(self, store):
        store.upsert(_inc("INC-1", "outage", root_cause="kernel OOM killer triggered"))
        assert {r["key"] for r in store.search("OOM")} == {"INC-1"}

    def test_empty_query_returns_empty_list(self, store):
        store.upsert(_inc("INC-1", "anything"))
        assert store.search("") == []
        assert store.search("   ") == []
        assert store.search("!!!") == []  # no tokens

    def test_limit_respected(self, store):
        for i in range(10):
            store.upsert(_inc(f"INC-{i}", "shared keyword incident"))
        assert len(store.search("keyword", limit=3)) == 3

    def test_special_chars_do_not_crash(self, store):
        store.upsert(_inc("INC-1", "quote \" and paren ( issue"))
        # Must not raise even with FTS metacharacters in the query.
        assert isinstance(store.search('"(' + " AND OR NOT *"), list)

    def test_search_after_close_degrades_not_raises(self, tmp_home):
        # A prefetch racing shutdown's close() must degrade to [], not raise a
        # ProgrammingError on the model-facing path (audit finding 4).
        db = os.path.join(tmp_home, "closed.db")
        s = IncidentStore(db)
        s.upsert(_inc("INC-1", "thing"))
        s.close()
        assert s.search("thing") == []


class TestChangeDetection:
    """upsert_many reports only genuinely new/changed rows, so a no-op
    re-ingest doesn't churn the index or signal a change (audit finding 3)."""

    def test_returns_count_of_new_rows(self, store):
        assert store.upsert_many([_inc("INC-1", "a"), _inc("INC-2", "b")]) == 2

    def test_unchanged_reupsert_counts_zero(self, store):
        store.upsert_many([_inc("INC-1", "a")])
        assert store.upsert_many([_inc("INC-1", "a")]) == 0  # identical -> no change

    def test_changed_field_counts_one_and_persists(self, store):
        store.upsert_many([_inc("INC-1", "a")])
        assert store.upsert_many([_inc("INC-1", "a-revised")]) == 1
        assert store.get("INC-1")["summary"] == "a-revised"

    def test_mixed_batch_counts_only_changed(self, store):
        store.upsert_many([_inc("INC-1", "a"), _inc("INC-2", "b")])
        n = store.upsert_many([
            _inc("INC-1", "a"),            # unchanged
            _inc("INC-2", "b-new"),        # changed
            _inc("INC-3", "c"),            # new
        ])
        assert n == 2

    def test_unchanged_reupsert_keeps_search_working(self, store):
        store.upsert_many([_inc("INC-1", "kafka consumer lag")])
        store.upsert_many([_inc("INC-1", "kafka consumer lag")])  # no-op
        assert {r["key"] for r in store.search("kafka")} == {"INC-1"}


class TestLinks:
    def test_record_and_fetch(self, store):
        link = store.record_link("sess-1", "INC-1", "investigating")
        assert link["id"] >= 1
        assert link["incident_key"] == "INC-1"
        fetched = store.links_for("INC-1")
        assert len(fetched) == 1
        assert fetched[0]["note"] == "investigating"


class TestWatermarkAndStats:
    def test_watermark_roundtrip(self, store):
        assert store.get_watermark() is None
        store.set_watermark("2024-01-02T03:04:05Z")
        assert store.get_watermark() == "2024-01-02T03:04:05Z"

    def test_stats(self, store):
        store.upsert(_inc("INC-1", "x"))
        store.record_link("s", "INC-1", "n")
        stats = store.stats()
        assert stats["incidents"] == 1
        assert stats["links"] == 1


class TestRetention:
    def test_prune_older_than(self, store):
        old = {**_inc("OLD-1", "ancient"), "updated": "2020-01-01T00:00:00.000+0000"}
        new = {**_inc("NEW-1", "recent"), "updated": "2099-01-01T00:00:00.000+0000"}
        store.upsert(old)
        store.upsert(new)
        removed = store.prune_older_than("2025-01-01")
        assert removed == 1
        assert store.get("OLD-1") is None
        assert store.get("NEW-1") is not None
        # FTS mirror was pruned too — the old row no longer matches.
        assert {r["key"] for r in store.search("ancient recent")} == {"NEW-1"}

    def test_prune_keeps_rows_without_updated(self, store):
        store.upsert(_inc("NODATE-1", "no timestamp"))  # updated == ""
        assert store.prune_older_than("2025-01-01") == 0
        assert store.get("NODATE-1") is not None


class TestFtsMirrorMaintenance:
    """Mirror rows are refreshed via an indexed MATCH + rowid delete, not a
    per-key full virtual-table scan under the store lock (perf finding)."""

    def test_fts_delete_plan_is_not_a_full_scan(self, store):
        store.upsert(_inc("INC-1", "x"))
        plan = " | ".join(
            row[3] for row in store._conn.execute(
                "EXPLAIN QUERY PLAN " + IncidentStore._FTS_DELETE_SQL,
                IncidentStore._fts_delete_params("INC-1"),
            ).fetchall()
        )
        # fts5 renders a full scan as a bare "VIRTUAL TABLE INDEX 0:"; the
        # MATCH-driven subquery plan carries an M constraint instead.
        assert "0:M" in plan, plan

    def test_upsert_replaces_fts_row_without_duplicates(self, store):
        store.upsert(_inc("INC-1", "first words"))
        store.upsert(_inc("INC-1", "second words"))
        n = store._conn.execute(
            "SELECT count(*) FROM incidents_fts WHERE key = ?", ("INC-1",)
        ).fetchone()[0]
        assert n == 1
        assert [r["key"] for r in store.search("second")] == ["INC-1"]
        assert store.search("first") == []

    def test_similar_keys_are_not_clobbered(self, store):
        # A phrase match for "INC-1" also hits "INC-1-EXTRA" (token prefix);
        # the exact-key filter must protect the neighbour's mirror row.
        store.upsert(_inc("INC-1", "alpha"))
        store.upsert(_inc("INC-1-EXTRA", "beta"))
        store.upsert(_inc("INC-1", "alpha revised"))
        assert {r["key"] for r in store.search("beta")} == {"INC-1-EXTRA"}
        assert {r["key"] for r in store.search("alpha")} == {"INC-1"}


class TestFtsUnavailableFallback:
    """On an FTS5-less SQLite the store must still open (fts_available=False)
    and search() must degrade to a LIKE scan with the same result shape —
    matching state.py's 'readers must keep a LIKE fallback' contract."""

    @pytest.fixture
    def nofts(self, tmp_home, monkeypatch):
        def no_fts5(self):
            raise sqlite3.OperationalError("no such module: fts5")
        monkeypatch.setattr(IncidentStore, "_create_fts", no_fts5)
        s = IncidentStore(os.path.join(tmp_home, "nofts.db"))
        yield s
        s.close()

    def test_store_opens_and_flags_fts_unavailable(self, nofts):
        assert nofts.fts_available is False

    def test_upsert_and_like_search(self, nofts):
        nofts.upsert(_inc("INC-1", "database pool exhausted"))
        nofts.upsert(_inc("INC-2", "payment gateway timeout"))
        rows = nofts.search("database")
        assert {r["key"] for r in rows} == {"INC-1"}
        # identical row shape to the FTS path
        assert set(rows[0]) == {"key", "summary", "status", "root_cause",
                                "reporter", "assignee", "created", "updated", "body"}

    def test_like_search_matches_body_and_root_cause(self, nofts):
        nofts.upsert(_inc("INC-1", "outage", root_cause="kernel OOM killer"))
        assert {r["key"] for r in nofts.search("OOM")} == {"INC-1"}

    def test_like_ranking_prefers_more_matched_tokens(self, nofts):
        nofts.upsert(_inc("INC-1", "kafka consumer lag", body="kafka"))
        nofts.upsert(_inc("INC-2", "consumer complaints"))
        assert nofts.search("kafka consumer")[0]["key"] == "INC-1"

    def test_like_wildcards_are_literal(self, nofts):
        nofts.upsert(_inc("INC-1", "failure_rate at 50%"))
        nofts.upsert(_inc("INC-2", "failureXrate"))
        # "_" in the query token must not act as a LIKE single-char wildcard.
        assert {r["key"] for r in nofts.search("failure_rate")} == {"INC-1"}
        assert nofts.search("") == []
        assert nofts.search("!!!") == []

    def test_upsert_many_prune_and_retention_work_without_fts(self, nofts):
        assert nofts.upsert_many([_inc("INC-1", "a"), _inc("INC-2", "b")]) == 2
        old = {**_inc("OLD-1", "ancient"), "updated": "2020-01-01T00:00:00.000+0000"}
        nofts.upsert(old)
        assert nofts.prune_older_than("2025-01-01") == 1
        assert nofts.delete_keys_not_in({"INC-1"}) == 1
        assert nofts.get("INC-2") is None
        assert nofts.get("INC-1") is not None


class TestDeleteKeysNotIn:
    """--full resync support: expunge keys Jira no longer returns."""

    def test_removes_unseen_keys_and_their_fts_rows(self, store):
        store.upsert(_inc("INC-1", "alpha thing"))
        store.upsert(_inc("INC-2", "beta thing"))
        removed = store.delete_keys_not_in({"INC-2"})
        assert removed == 1
        assert store.get("INC-1") is None
        assert store.get("INC-2") is not None
        assert {r["key"] for r in store.search("thing")} == {"INC-2"}
        assert store.count() == 1

    def test_noop_when_everything_was_seen(self, store):
        store.upsert(_inc("INC-1", "x"))
        assert store.delete_keys_not_in({"INC-1"}) == 0
        assert store.count() == 1


# ---------------------------------------------------------------------------
# Security: the index holds unredacted PII, so it must not be world/group
# readable at rest (audit finding 3).
# ---------------------------------------------------------------------------

class TestAtRestPermissions:
    def test_db_file_is_owner_only(self, tmp_path):
        import stat
        db = tmp_path / "nested" / "incidents.db"
        store = IncidentStore(str(db))
        try:
            store.upsert(_inc("INC-1", "x"))
            mode = stat.S_IMODE(os.stat(str(db)).st_mode)
            assert mode == 0o600, oct(mode)
            # Sidecar files (if any) must be locked down too.
            for suffix in ("-wal", "-shm", "-journal"):
                p = str(db) + suffix
                if os.path.exists(p):
                    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        finally:
            store.close()

    def test_new_parent_dir_is_owner_only(self, tmp_path):
        import stat
        db = tmp_path / "fresh_dir" / "incidents.db"
        store = IncidentStore(str(db))
        try:
            mode = stat.S_IMODE(os.stat(str(db.parent)).st_mode)
            assert mode == 0o700, oct(mode)
        finally:
            store.close()
