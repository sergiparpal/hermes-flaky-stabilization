"""SQLite store tests: §4.6 schema, WAL, runs/audit/recipes round-trips."""

import sqlite3

import pytest
from flaky_healer.recipes.store import HealerStore


@pytest.fixture
def store(tmp_path):
    return HealerStore(tmp_path / "healer.db")


def test_schema_matches_plan(store):
    conn = sqlite3.connect(store.db_path)
    def cols(table):
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

    # Unified-plugin adaptation (plan Appendix A): the runs table is named
    # healer_runs inside the consolidated state.db.
    assert cols("healer_runs") == [
        "id", "ts", "source", "build_id", "test_id", "diagnosis_json",
        "strategy", "result", "isolation", "duration_s",
    ]
    assert cols("recipes") == [
        "signature", "created_ts", "diagnosis_json", "strategy", "patch_ops_json",
        "hits", "successes", "failures", "last_used_ts", "relaxed_key",
    ]
    assert cols("audit") == ["id", "ts", "event", "payload_json"]
    conn.close()


def test_wal_mode_enabled(store):
    store.audit("probe", {})  # force a connection that sets the journal mode
    conn = sqlite3.connect(store.db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_migration_idempotent(tmp_path):
    path = tmp_path / "healer.db"
    HealerStore(path)
    store = HealerStore(path)  # re-open: must not fail or duplicate
    assert store.all_recipes() == []


def test_record_run_roundtrip(store):
    run_id = store.record_run(
        source="local",
        build_id=None,
        test_id="tests/x.spec.ts",
        diagnosis={"cause": "timeout"},
        strategy="bump_timeout",
        result="stable",
        isolation="docker",
        duration_s=12.345,
    )
    assert run_id == 1
    runs = store.runs()
    assert len(runs) == 1
    assert runs[0]["diagnosis"] == {"cause": "timeout"}
    assert runs[0]["result"] == "stable"
    assert runs[0]["isolation"] == "docker"


def test_audit_roundtrip_and_unserializable_payload(store):
    store.audit("pre_approval_request", {"tool": "terminal"})
    store.audit("post_approval_response", object())  # not JSON-serializable
    rows = store.audit_rows()
    assert [r["event"] for r in rows] == ["post_approval_response", "pre_approval_request"]
    assert rows[1]["payload"] == {"tool": "terminal"}
    assert "repr" in rows[0]["payload"]


def test_recipe_upsert_get_bump(store):
    parts = {"framework": "playwright", "error_class": "TimeoutError"}
    store.upsert_recipe(
        "sig-1",
        diagnosis={"cause": "timeout"},
        strategy="bump_timeout",
        patch_ops=[{"op": "replace", "file": "t.ts", "anchor": "a", "old": "x", "new": "y"}],
        signature_parts=parts,
    )
    recipe = store.get_recipe("sig-1")
    assert recipe.strategy == "bump_timeout"
    assert recipe.diagnosis == {"cause": "timeout"}
    assert recipe.signature_parts == parts
    assert recipe.hits == recipe.successes == recipe.failures == 0

    store.bump_recipe("sig-1", success=True)
    store.bump_recipe("sig-1", success=False)
    recipe = store.get_recipe("sig-1")
    assert (recipe.hits, recipe.successes, recipe.failures) == (2, 1, 1)
    assert recipe.last_used_ts is not None


def test_recipe_upsert_refresh_with_same_body_preserves_counters(store):
    store.upsert_recipe(
        "sig-2", diagnosis={"cause": "timeout"}, strategy="bump_timeout",
        patch_ops=[{"op": "replace"}], signature_parts={},
    )
    store.bump_recipe("sig-2", success=True)
    store.upsert_recipe(
        "sig-2", diagnosis={"cause": "timeout", "v": 2}, strategy="bump_timeout",
        patch_ops=[{"op": "replace"}], signature_parts={},
    )
    recipe = store.get_recipe("sig-2")
    assert recipe.hits == 1 and recipe.successes == 1, "same-body refresh keeps counters"
    assert recipe.last_used_ts is not None
    assert recipe.diagnosis["v"] == 2


def test_get_missing_recipe_is_none(store):
    assert store.get_recipe("nope") is None


def test_relaxed_key_indexed_lookup_and_null_is_skipped(store):
    from flaky_healer.recipes.signature import relaxed_key, signature_hash, signature_parts

    parts = signature_parts(
        error_class="E", failing_action="click", selector="#a", message="m"
    )
    sig = signature_hash(parts)
    store.upsert_recipe(
        sig, diagnosis={"cause": "timeout"}, strategy="bump_timeout",
        patch_ops=[], signature_parts=parts,
    )
    assert [r.signature for r in store.recipes_by_relaxed_key(relaxed_key(parts))] == [sig]
    # a recipe stored without signature_parts gets a NULL key and never matches
    store.upsert_recipe(
        "plain", diagnosis={"cause": "x"}, strategy="s", patch_ops=[], signature_parts={}
    )
    assert store.recipes_by_relaxed_key(relaxed_key({})) == []


def test_v1_to_v2_migration_adds_and_backfills_relaxed_key(tmp_path):
    import json

    from flaky_healer.recipes.signature import relaxed_key, signature_parts

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(  # a v1-shaped DB: recipes table without relaxed_key
        "CREATE TABLE recipes(signature TEXT PRIMARY KEY, created_ts TEXT,"
        " diagnosis_json TEXT, strategy TEXT, patch_ops_json TEXT, hits INTEGER DEFAULT 0,"
        " successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0, last_used_ts TEXT);"
        "CREATE TABLE runs(id INTEGER PRIMARY KEY);"
        "CREATE TABLE audit(id INTEGER PRIMARY KEY);"
    )
    parts = signature_parts(
        error_class="E", failing_action="click", selector="#a", message="m"
    )
    conn.execute(
        "INSERT INTO recipes(signature, created_ts, diagnosis_json, strategy, patch_ops_json)"
        " VALUES(?,?,?,?,?)",
        ("legacy-sig", "2020-01-01", json.dumps({"diagnosis": {}, "signature_parts": parts}),
         "bump_timeout", "[]"),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    # Unified-plugin adaptation: the store no longer owns migrations — opening
    # a legacy v1-shaped DB must tolerate the missing relaxed_key column (added
    # as NULL, which relaxed matching skips: the legacy semantics for
    # un-backfilled rows). The real v1→v2 relaxed_key BACKFILL now lives in the
    # legacy-data importer (`flaky-stab migrate`, tests/test_migrate.py).
    store = HealerStore(path)
    assert store.recipes_by_relaxed_key(relaxed_key(parts)) == []
    assert store.get_recipe("legacy-sig").signature == "legacy-sig"
    # New writes get relaxed_key immediately (v2 semantics built into state.db).
    store.upsert_recipe("new-sig", diagnosis={}, strategy="bump_timeout",
                        patch_ops=[], signature_parts=parts)
    assert [r.signature for r in store.recipes_by_relaxed_key(relaxed_key(parts))] == [
        "new-sig"
    ]
