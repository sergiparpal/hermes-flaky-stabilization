"""``state.db`` — one SQLite file for every private table (plan Appendix A, D4).

Holds: detective verdicts + scan runs, triage patterns (+FTS), healer
runs/recipes/audit, the incidents index (+FTS) with links/meta, and the
orchestrator's ``pipeline_runs`` ledger. The public
``<hermes_home>/test-history/history.db`` is *not* here by design — it is a
cross-plugin file-level contract owned by the ``history`` stage.

Migration ladder (healer pattern): each ``SCHEMA_VERSION`` bump appends an
``if version < N`` step; recorded versions live in the ``schema_version``
table so migrations survive file copies (unlike ``PRAGMA user_version``,
which ``VACUUM INTO`` and some backup tools drop).

Legacy column sets are preserved exactly so the Phase-7 legacy-data migration
is a plain ``INSERT ... SELECT``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import paths

STATE_DB_NAME = "state.db"

SCHEMA_VERSION = 1

_V1_DDL = (
    # -- detective (from verdicts.db) -----------------------------------------
    """CREATE TABLE IF NOT EXISTS flaky_verdicts(
        test_key TEXT PRIMARY KEY, classname TEXT, name TEXT NOT NULL, file_path TEXT,
        passes INTEGER NOT NULL, fails INTEGER NOT NULL, runs INTEGER NOT NULL,
        window_days INTEGER NOT NULL, first_seen TIMESTAMP, last_seen TIMESTAMP,
        last_failure TIMESTAMP, status TEXT NOT NULL,
        computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_verdicts_status ON flaky_verdicts(status)",
    "CREATE INDEX IF NOT EXISTS idx_verdicts_name   ON flaky_verdicts(name)",
    """CREATE TABLE IF NOT EXISTS scan_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ran_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        window_days INTEGER NOT NULL, min_fails INTEGER NOT NULL,
        include_errors INTEGER NOT NULL, source_schema_version INTEGER,
        tests_examined INTEGER NOT NULL, flaky_found INTEGER NOT NULL)""",
    # -- triage (from ci_triage_patterns.db; renamed patterns -> triage_patterns)
    """CREATE TABLE IF NOT EXISTS triage_patterns(
        project TEXT NOT NULL, signature TEXT NOT NULL, category TEXT NOT NULL,
        occurrences INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        sample TEXT NOT NULL DEFAULT '', PRIMARY KEY (project, signature))""",
    """CREATE INDEX IF NOT EXISTS idx_triage_patterns_last_seen
        ON triage_patterns(project, last_seen)""",
    # -- healer (from healer.db; runs renamed healer_runs; v2 relaxed_key included)
    """CREATE TABLE IF NOT EXISTS healer_runs(
        id INTEGER PRIMARY KEY, ts TEXT, source TEXT, build_id TEXT, test_id TEXT,
        diagnosis_json TEXT, strategy TEXT, result TEXT, isolation TEXT, duration_s REAL)""",
    """CREATE TABLE IF NOT EXISTS recipes(
        signature TEXT PRIMARY KEY, created_ts TEXT, diagnosis_json TEXT, strategy TEXT,
        patch_ops_json TEXT, hits INTEGER DEFAULT 0, successes INTEGER DEFAULT 0,
        failures INTEGER DEFAULT 0, last_used_ts TEXT, relaxed_key TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_recipes_relaxed_key ON recipes(relaxed_key)",
    """CREATE TABLE IF NOT EXISTS audit(
        id INTEGER PRIMARY KEY, ts TEXT, event TEXT, payload_json TEXT)""",
    # -- incidents (from hermes-jira-incidents.db) -----------------------------
    """CREATE TABLE IF NOT EXISTS incidents(
        key TEXT PRIMARY KEY, summary TEXT, status TEXT, root_cause TEXT, reporter TEXT,
        assignee TEXT, created TEXT, updated TEXT, body TEXT, raw_json TEXT, indexed_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_incidents_updated ON incidents(updated)",
    """CREATE TABLE IF NOT EXISTS links(
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, incident_key TEXT,
        note TEXT, created_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",
    # -- orchestrator (new) ----------------------------------------------------
    """CREATE TABLE IF NOT EXISTS pipeline_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, trigger TEXT NOT NULL,
        test_id TEXT, project TEXT, triage_category TEXT, branch TEXT,
        outcome TEXT NOT NULL, stage_results_json TEXT NOT NULL, duration_s REAL)""",
)

# FTS5 mirrors — created best-effort; readers must keep a LIKE fallback.
_V1_FTS = (
    """CREATE VIRTUAL TABLE IF NOT EXISTS triage_patterns_fts USING fts5(
        sample, project UNINDEXED, signature UNINDEXED)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS incidents_fts USING fts5(
        key, summary, status, root_cause, body, tokenize='unicode61')""",
)


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    for ddl in _V1_DDL:
        if "idx_recipes_relaxed_key" in ddl:
            # Tolerate a pre-existing legacy (healer v1) recipes table without
            # the relaxed_key column: CREATE TABLE IF NOT EXISTS above no-ops
            # on it, and creating the index would then fail. Adding the column
            # (NULL — invisible to relaxed matching, the legacy semantics for
            # un-backfilled rows) keeps the ladder idempotent; the real
            # backfill belongs to the legacy-data importer (plan Phase 7).
            cols = [r[1] for r in conn.execute("PRAGMA table_info(recipes)")]
            if cols and "relaxed_key" not in cols:
                conn.execute("ALTER TABLE recipes ADD COLUMN relaxed_key TEXT")
        conn.execute(ddl)
    for ddl in _V1_FTS:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            # FTS5 not compiled in — LIKE fallbacks take over at query time.
            pass


# Ordered ladder: (target_version, step). Each step must be idempotent.
_MIGRATIONS: tuple[tuple[int, object], ...] = ((1, _migrate_to_v1),)


def state_db_path(data_dir: Path | None = None) -> Path:
    return (data_dir or paths.get_data_dir()) / STATE_DB_NAME


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    value = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    return int(value or 0)


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = current_version(conn)
    if version >= SCHEMA_VERSION:
        return
    with conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_version(
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        for target, step in _MIGRATIONS:
            if version < target:
                step(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (target,)
                )


def fts_available(conn: sqlite3.Connection) -> bool:
    """True when the v1 FTS virtual tables were created (FTS5 compiled in)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidents_fts'"
    ).fetchone()
    return row is not None


def connect(db_path: Path | str | None = None, *, uri: bool = False) -> sqlite3.Connection:
    """Open ``state.db`` with the schema ensured.

    WAL journal mode (with silent fallback for filesystems that refuse it),
    ``timeout=15`` for cross-process writer contention,
    ``check_same_thread=False`` because tools may run in the gateway's async
    worker pool, owner-only file permissions. Callers own closing (short-lived
    connections via ``contextlib.closing`` are the house style).

    ``uri=True`` sets ``SQLITE_OPEN_URI`` on the connection so that later
    ``ATTACH 'file:…?mode=ro'`` filenames are honored regardless of the
    build's ``SQLITE_USE_URI`` default — the legacy migration relies on this
    for its strictly read-only source attaches. Plain paths (which never
    start with ``file:``) are unaffected by the flag.
    """
    from .db import open_private

    path = Path(db_path) if db_path is not None else state_db_path()
    # The shared private-WAL recipe owns precreate-0600 + connect + WAL; state.db
    # (incidents, triage samples, healer audit — all sensitive) is never exposed
    # at the umask.
    conn = open_private(path, uri=uri, check_same_thread=False, timeout=15)
    ensure_schema(conn)
    # After schema init the WAL/-shm sidecars exist; lock the db and all sidecars
    # to 0600 (restrict_file alone missed the sidecars).
    paths.harden_db_files(path)
    return conn
