"""Repository for the orchestrator's ``pipeline_runs`` ledger.

The orchestrator decides *what* to record (the fork branch, the outcome, the
per-stage results); persistence is here, so the orchestrator holds no SQL/DDL.
The table's DDL is owned by :mod:`state` (the single ``state.db`` schema owner);
this module owns only the one INSERT.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from . import state

_INSERT = (
    "INSERT INTO pipeline_runs (ts, trigger, test_id, project, triage_category,"
    " branch, outcome, stage_results_json, duration_s)"
    " VALUES (?,?,?,?,?,?,?,?,?)"
)


def record_run(row: dict[str, Any]) -> None:
    """Append one run to the ledger. Missing fields default as they did inline."""
    with closing(state.connect()) as conn, conn:
        conn.execute(
            _INSERT,
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                row.get("trigger", "tool"),
                row.get("test_id"),
                row.get("project"),
                row.get("triage_category"),
                row.get("branch"),
                row.get("outcome", ""),
                row.get("stage_results_json", "{}"),
                row.get("duration_s"),
            ),
        )
