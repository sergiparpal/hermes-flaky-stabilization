"""SQLite schema, FTS5 sync triggers, and forward-compatible migrations.

The data model is three tables plus one FTS5 virtual table:

* ``test_runs``      — one row per ingested JUnit XML file.
* ``test_cases``     — one row per ``<testcase>`` within a run.
* ``test_cases_fts`` — external-content FTS5 index over the searchable columns
  of ``test_cases`` (kept in sync by the triggers below).
* ``schema_version`` — single-row version marker for future migrations.

All DDL is idempotent (``IF NOT EXISTS`` / ``INSERT OR IGNORE``), so
``apply_schema`` can run on every connection open without harm.
"""

import sqlite3

# Current schema version. Bump this and add migration steps in ``apply_schema``
# when the DDL changes in a backward-incompatible way.
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS test_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    suite_name    TEXT NOT NULL,
    ingested_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    run_timestamp TIMESTAMP,                       -- from XML if present
    total         INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    source_file   TEXT NOT NULL                    -- absolute path to ingested XML
);

CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON test_runs(run_timestamp);

CREATE TABLE IF NOT EXISTS test_cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    classname       TEXT,
    name            TEXT NOT NULL,
    file_path       TEXT,                          -- may be null (some emitters omit)
    line_number     INTEGER,
    status          TEXT NOT NULL,                 -- 'passed' | 'failed' | 'error' | 'skipped'
    duration_ms     REAL,
    failure_message TEXT,
    failure_type    TEXT,
    stack_trace     TEXT
);

CREATE INDEX IF NOT EXISTS idx_cases_run     ON test_cases(run_id);
CREATE INDEX IF NOT EXISTS idx_cases_name    ON test_cases(name);
CREATE INDEX IF NOT EXISTS idx_cases_module  ON test_cases(file_path);
CREATE INDEX IF NOT EXISTS idx_cases_status  ON test_cases(status);

CREATE VIRTUAL TABLE IF NOT EXISTS test_cases_fts USING fts5(
    classname,
    name,
    failure_message,
    stack_trace,
    content='test_cases',
    content_rowid='id'
);

-- FTS5 external-content sync triggers. They keep test_cases_fts consistent with
-- test_cases on every insert/update/delete (including FK-cascade deletes when
-- recursive_triggers is enabled — see storage.get_connection).
CREATE TRIGGER IF NOT EXISTS test_cases_ai AFTER INSERT ON test_cases BEGIN
    INSERT INTO test_cases_fts(rowid, classname, name, failure_message, stack_trace)
    VALUES (new.id, new.classname, new.name, new.failure_message, new.stack_trace);
END;

CREATE TRIGGER IF NOT EXISTS test_cases_ad AFTER DELETE ON test_cases BEGIN
    INSERT INTO test_cases_fts(test_cases_fts, rowid, classname, name, failure_message, stack_trace)
    VALUES ('delete', old.id, old.classname, old.name, old.failure_message, old.stack_trace);
END;

CREATE TRIGGER IF NOT EXISTS test_cases_au AFTER UPDATE ON test_cases BEGIN
    INSERT INTO test_cases_fts(test_cases_fts, rowid, classname, name, failure_message, stack_trace)
    VALUES ('delete', old.id, old.classname, old.name, old.failure_message, old.stack_trace);
    INSERT INTO test_cases_fts(rowid, classname, name, failure_message, stack_trace)
    VALUES (new.id, new.classname, new.name, new.failure_message, new.stack_trace);
END;

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Seed the baseline version that the CREATE statements above represent. This is
-- deliberately the literal 1, NOT SCHEMA_VERSION: future migrations bump
-- SCHEMA_VERSION and each records its own row here (gated on current_version), so
-- this baseline seed must stay pinned at 1 even as SCHEMA_VERSION advances.
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
"""


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create all tables, indexes, the FTS5 table, and sync triggers if missing.

    Idempotent: safe to call on every connection open. When the schema later
    grows past version 1, add ``ALTER``/data-migration steps here gated on
    ``current_version(conn)``.
    """
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or 0 if none recorded yet."""
    try:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        # schema_version table does not exist yet.
        return 0
    return int(row[0]) if row and row[0] is not None else 0
