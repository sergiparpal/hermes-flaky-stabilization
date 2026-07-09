"""Verdicts-database schema and forward-compatible migrations.

Two data tables plus a version marker:

* ``flaky_verdicts`` — one row per test, the latest classification (overwritten
  wholesale by each scan).
* ``scan_runs``      — one append-only row per scan, for ``status``/auditing.
* ``schema_version`` — single-row version marker for future migrations.

All DDL is idempotent (``IF NOT EXISTS`` / ``INSERT OR IGNORE``), so
``apply_schema`` can run on every connection open without harm. This is *this*
plugin's own schema (distinct from the test-history schema it reads).
"""

import sqlite3

# Current verdicts-DB schema version. Bump and add migration steps in
# ``apply_schema`` when the DDL changes incompatibly.
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS flaky_verdicts (
    test_key     TEXT PRIMARY KEY,
    classname    TEXT,
    name         TEXT NOT NULL,
    file_path    TEXT,
    passes       INTEGER NOT NULL,
    fails        INTEGER NOT NULL,
    runs         INTEGER NOT NULL,
    window_days  INTEGER NOT NULL,
    first_seen   TIMESTAMP,
    last_seen    TIMESTAMP,
    last_failure TIMESTAMP,
    status       TEXT NOT NULL,            -- 'flaky' | 'consistently_failing' | 'stable'
    computed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_verdicts_status ON flaky_verdicts(status);
-- The is_flaky tool resolves by bare `name` (and file_path::name) when the
-- caller has no exact classname::name key — the common triage case. Without this
-- index those lookups scan the whole table on the latency-sensitive tool path.
-- Selective enough that the OR on classname/file_path filters the tiny matched set.
CREATE INDEX IF NOT EXISTS idx_verdicts_name ON flaky_verdicts(name);

CREATE TABLE IF NOT EXISTS scan_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    window_days           INTEGER NOT NULL,
    min_fails             INTEGER NOT NULL,
    include_errors        INTEGER NOT NULL,
    source_schema_version INTEGER,         -- test-history schema_version observed
    tests_examined        INTEGER NOT NULL,
    flaky_found           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Baseline version this DDL represents. Deliberately the literal 1, NOT
-- SCHEMA_VERSION: future migrations bump SCHEMA_VERSION and record their own row.
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
"""


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create both tables and the version marker if missing. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or 0 if none recorded yet."""
    try:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0
