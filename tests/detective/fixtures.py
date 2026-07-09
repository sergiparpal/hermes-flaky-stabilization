"""Helpers to build a synthetic ``hermes-test-history`` database for tests.

The DDL below re-implements the subset of test-history's schema that this
plugin's reader touches (``test_runs``, ``test_cases``, ``schema_version``). It
is **re-created here, never imported** from test-history — reading its data file
is fine, but importing its GPL-3.0 Python would relicense this plugin (§3.1).

A run is a dict::

    {
      "source_file": "ci/junit.xml",       # required
      "run_timestamp": "2026-05-20T09:00:00",  # ISO; None to test the ingested_at fallback
      "ingested_at": "2026-05-20 09:05:00",    # optional; omit to let CURRENT_TIMESTAMP apply
      "suite_name": "suite",                # optional
      "cases": [ {"name": ..., "status": ..., "classname": ..., "file_path": ...}, ... ],
    }
"""

import sqlite3
from pathlib import Path

# Mirrors hermes-test-history/schema.py (the columns this plugin reads). FTS5
# table + triggers are intentionally omitted: the reader never queries them.
_DDL = """
CREATE TABLE IF NOT EXISTS test_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    suite_name    TEXT NOT NULL,
    ingested_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    run_timestamp TIMESTAMP,
    total         INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    source_file   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    classname       TEXT,
    name            TEXT NOT NULL,
    file_path       TEXT,
    line_number     INTEGER,
    status          TEXT NOT NULL,
    duration_ms     REAL,
    failure_message TEXT,
    failure_type    TEXT,
    stack_trace     TEXT
);
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def build_test_history_db(path, runs, *, schema_version: int | None = 1) -> Path:
    """Create a synthetic test-history DB at ``path`` seeded with ``runs``.

    ``schema_version=None`` leaves the ``schema_version`` table empty (to exercise
    the reader's "no version row" warning path).
    """
    path = Path(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_DDL)
        if schema_version is not None:
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (schema_version,),
            )
        for run in runs:
            fields = ["suite_name", "run_timestamp", "total", "failures",
                      "errors", "skipped", "source_file"]
            values = [
                run.get("suite_name", "suite"),
                run.get("run_timestamp"),
                run.get("total", 0),
                run.get("failures", 0),
                run.get("errors", 0),
                run.get("skipped", 0),
                run["source_file"],
            ]
            # Only bind ingested_at when provided; otherwise let the column
            # DEFAULT (CURRENT_TIMESTAMP) apply rather than overwriting it NULL.
            if run.get("ingested_at") is not None:
                fields.append("ingested_at")
                values.append(run["ingested_at"])
            placeholders = ", ".join("?" * len(fields))
            cur = conn.execute(
                f"INSERT INTO test_runs ({', '.join(fields)}) VALUES ({placeholders})",
                values,
            )
            run_id = cur.lastrowid
            for case in run["cases"]:
                conn.execute(
                    "INSERT INTO test_cases (run_id, classname, name, file_path, "
                    "line_number, status, duration_ms, failure_message, "
                    "failure_type, stack_trace) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        case.get("classname"),
                        case["name"],
                        case.get("file_path"),
                        case.get("line_number"),
                        case["status"],
                        case.get("duration_ms"),
                        case.get("failure_message"),
                        case.get("failure_type"),
                        case.get("stack_trace"),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return path
