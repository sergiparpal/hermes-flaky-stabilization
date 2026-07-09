"""Schema application, versioning, and FTS5 sync triggers (Phase 1 acceptance)."""

import sqlite3

from hermes_test_history import schema


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    return conn


def test_apply_schema_creates_all_objects():
    conn = _conn()
    schema.apply_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"test_runs", "test_cases", "test_cases_fts", "schema_version"} <= tables
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert triggers == {"test_cases_ai", "test_cases_ad", "test_cases_au"}


def test_apply_schema_is_idempotent():
    conn = _conn()
    schema.apply_schema(conn)
    schema.apply_schema(conn)  # second run must not raise
    assert schema.current_version(conn) == schema.SCHEMA_VERSION == 1


def test_current_version_zero_when_unapplied():
    conn = _conn()
    assert schema.current_version(conn) == 0  # schema_version table absent yet


def test_fts_insert_then_delete_stays_in_sync():
    conn = _conn()
    schema.apply_schema(conn)
    conn.execute("INSERT INTO test_runs (suite_name, source_file) VALUES ('s', 'x')")
    conn.execute(
        "INSERT INTO test_cases (run_id, name, status, failure_message) "
        "VALUES (1, 'test_oauth_login', 'failed', 'token expired')"
    )
    match = "SELECT COUNT(*) FROM test_cases_fts WHERE test_cases_fts MATCH ?"
    assert conn.execute(match, ("oauth",)).fetchone()[0] == 1
    assert conn.execute(match, ("token",)).fetchone()[0] == 1  # indexes failure_message
    conn.execute("DELETE FROM test_cases WHERE name = 'test_oauth_login'")
    assert conn.execute(match, ("oauth",)).fetchone()[0] == 0  # delete-trigger fired
