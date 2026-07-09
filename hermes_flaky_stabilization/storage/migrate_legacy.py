"""Copy-based migration of the four legacy plugin DBs into ``state.db``
(plan Phase 7 / §10 / Appendix C).

* ``<home>/flaky-detective/verdicts.db``        → ``flaky_verdicts``, ``scan_runs``
* ``<home>/cache/ci_triage_patterns.db``        → ``triage_patterns`` (+FTS rebuild)
* ``<home>/plugins-data/hermes-flaky-healer/healer.db``
                                                → ``healer_runs``, ``recipes``, ``audit``
                                                  (v1 recipes get the v2
                                                  ``relaxed_key`` backfill here)
* ``<home>/hermes-jira-incidents.db``           → ``incidents`` (+FTS), ``links``, ``meta``

Sources are attached read-only (``mode=ro`` URI, ``immutable`` fallback off)
and **never modified** — rollback is trivial because nothing moves. Rows copy
via ``INSERT OR IGNORE … SELECT`` so pre-existing unified rows are never
clobbered. Idempotency is anchored on provenance: each completed source writes
a ``meta`` row ``migrated_from_<name> = <path> @ <iso-ts> rows=<n>`` and is
skipped on re-runs, so running migrate twice adds nothing.

``history.db`` needs no migration by design — unchanged path and schema (D2).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import paths
from . import state


@dataclass
class SourceReport:
    name: str
    path: str
    found: bool = False
    skipped: str | None = None
    tables: dict[str, int] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(self.tables.values())


def default_sources(home: Path) -> dict[str, Path]:
    return {
        "flaky_detective": home / "flaky-detective" / "verdicts.db",
        "ci_triage": home / "cache" / "ci_triage_patterns.db",
        "flaky_healer": home / "plugins-data" / "hermes-flaky-healer" / "healer.db",
        "jira_incidents": home / "hermes-jira-incidents.db",
    }


def _attach_ro(conn: sqlite3.Connection, path: Path) -> None:
    """Attach *path* strictly read-only (the source must never be modified)."""
    uri = f"file:{path}?mode=ro"
    try:
        conn.execute("ATTACH DATABASE ? AS legacy", (uri,))
        return
    except sqlite3.OperationalError:
        pass
    # Older SQLite builds may not accept URI filenames in ATTACH parameters;
    # plain attach still only ever READS legacy.* below.
    conn.execute("ATTACH DATABASE ? AS legacy", (str(path),))


def _detach(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("DETACH DATABASE legacy")
    except sqlite3.Error:
        pass


def _legacy_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM legacy.sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _legacy_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA legacy.table_info({table})")]


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM legacy.{table}").fetchone()[0])


def _copy_select(conn: sqlite3.Connection, target: str, columns: list[str],
                 source: str) -> int:
    cols = ", ".join(columns)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {target} ({cols}) SELECT {cols} FROM legacy.{source}"
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


# -- per-source copy steps ------------------------------------------------------


def _copy_detective(conn: sqlite3.Connection, report: SourceReport) -> None:
    tables = _legacy_tables(conn)
    if "flaky_verdicts" in tables:
        report.tables["flaky_verdicts"] = _copy_select(
            conn, "flaky_verdicts",
            ["test_key", "classname", "name", "file_path", "passes", "fails", "runs",
             "window_days", "first_seen", "last_seen", "last_failure", "status",
             "computed_at"],
            "flaky_verdicts")
    if "scan_runs" in tables:
        report.tables["scan_runs"] = _copy_select(
            conn, "scan_runs",
            ["id", "ran_at", "window_days", "min_fails", "include_errors",
             "source_schema_version", "tests_examined", "flaky_found"],
            "scan_runs")


def _copy_triage(conn: sqlite3.Connection, report: SourceReport) -> None:
    tables = _legacy_tables(conn)
    if "patterns" not in tables:
        return
    report.tables["triage_patterns"] = _copy_select(
        conn, "triage_patterns",
        ["project", "signature", "category", "occurrences", "first_seen",
         "last_seen", "sample"],
        "patterns")
    if state.fts_available(conn):
        conn.execute(
            "INSERT INTO triage_patterns_fts (sample, project, signature) "
            "SELECT sample, project, signature FROM triage_patterns p "
            "WHERE NOT EXISTS (SELECT 1 FROM triage_patterns_fts f "
            "WHERE f.project = p.project AND f.signature = p.signature)"
        )


def _healer_relaxed_key(diagnosis_json: str | None) -> str | None:
    """The legacy v1→v2 backfill knowledge (plan Phase 4/7): recover
    signature_parts from the stored blob and compute the indexed relaxed key;
    rows without parts stay NULL (invisible to relaxed matching)."""
    from ..healer.flaky_healer.recipes.signature import relaxed_key

    try:
        blob = json.loads(diagnosis_json or "{}")
    except Exception:
        return None
    parts = blob.get("signature_parts") or {} if "diagnosis" in blob else {}
    return relaxed_key(parts) if parts else None


def _copy_healer(conn: sqlite3.Connection, report: SourceReport) -> None:
    tables = _legacy_tables(conn)
    if "runs" in tables:
        report.tables["healer_runs"] = _copy_select(
            conn, "healer_runs",
            ["id", "ts", "source", "build_id", "test_id", "diagnosis_json",
             "strategy", "result", "isolation", "duration_s"],
            "runs")
    if "recipes" in tables:
        legacy_cols = _legacy_columns(conn, "recipes")
        has_relaxed = "relaxed_key" in legacy_cols
        copied = 0
        rows = conn.execute("SELECT * FROM legacy.recipes").fetchall()
        for row in rows:
            data = dict(row)
            rkey = data.get("relaxed_key") if has_relaxed else None
            if rkey is None:
                rkey = _healer_relaxed_key(data.get("diagnosis_json"))
            cur = conn.execute(
                "INSERT OR IGNORE INTO recipes (signature, created_ts, diagnosis_json,"
                " strategy, patch_ops_json, hits, successes, failures, last_used_ts,"
                " relaxed_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (data.get("signature"), data.get("created_ts"),
                 data.get("diagnosis_json"), data.get("strategy"),
                 data.get("patch_ops_json"), data.get("hits", 0),
                 data.get("successes", 0), data.get("failures", 0),
                 data.get("last_used_ts"), rkey),
            )
            if cur.rowcount and cur.rowcount > 0:
                copied += 1
        report.tables["recipes"] = copied
    if "audit" in tables:
        report.tables["audit"] = _copy_select(
            conn, "audit", ["id", "ts", "event", "payload_json"], "audit")


def _copy_incidents(conn: sqlite3.Connection, report: SourceReport) -> None:
    tables = _legacy_tables(conn)
    if "incidents" in tables:
        report.tables["incidents"] = _copy_select(
            conn, "incidents",
            ["key", "summary", "status", "root_cause", "reporter", "assignee",
             "created", "updated", "body", "raw_json", "indexed_at"],
            "incidents")
        if state.fts_available(conn):
            conn.execute(
                "INSERT INTO incidents_fts (key, summary, status, root_cause, body) "
                "SELECT key, summary, status, root_cause, body FROM incidents i "
                "WHERE NOT EXISTS (SELECT 1 FROM incidents_fts f WHERE f.key = i.key)"
            )
    if "links" in tables:
        report.tables["links"] = _copy_select(
            conn, "links",
            ["id", "session_id", "incident_key", "note", "created_at"], "links")
    if "meta" in tables:
        report.tables["meta"] = _copy_select(conn, "meta", ["key", "value"], "meta")


_COPIERS = {
    "flaky_detective": _copy_detective,
    "ci_triage": _copy_triage,
    "flaky_healer": _copy_healer,
    "jira_incidents": _copy_incidents,
}


# -- driver ----------------------------------------------------------------------


def _provenance_key(name: str) -> str:
    return f"migrated_from_{name}"


def migrate(home: Path | None = None, *, dry_run: bool = False,
            overrides: dict[str, Path] | None = None) -> list[SourceReport]:
    """Run the copy migration; return one report per legacy source.

    ``overrides`` may repoint individual source paths (e.g. a healer data dir
    that was relocated via ``FLAKY_HEALER_DATA_DIR``).
    """
    home = Path(home) if home else paths.get_hermes_home()
    sources = default_sources(home)
    for name, path in (overrides or {}).items():
        if name in sources and path:
            sources[name] = Path(path)

    reports: list[SourceReport] = []
    with closing(state.connect()) as conn:
        for name, path in sources.items():
            report = SourceReport(name=name, path=str(path))
            reports.append(report)
            if not path.exists():
                report.skipped = "source not found"
                continue
            report.found = True
            marker = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (_provenance_key(name),)
            ).fetchone()
            if marker is not None:
                report.skipped = f"already migrated ({marker[0]})"
                continue
            if dry_run:
                _attach_ro(conn, path)
                try:
                    for table in sorted(_legacy_tables(conn)):
                        # Skip FTS virtual tables AND their shadow tables
                        # (_fts_data/_fts_idx/…), sqlite internals, and the
                        # per-plugin version marker.
                        if "_fts" in table or table.startswith("sqlite_") \
                                or table == "schema_version":
                            continue
                        report.tables[table] = _count(conn, table)
                finally:
                    _detach(conn)
                report.skipped = "dry-run (nothing written)"
                continue
            _attach_ro(conn, path)
            try:
                with conn:
                    _COPIERS[name](conn, report)
                    stamp = datetime.now(UTC).isoformat(timespec="seconds")
                    conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (_provenance_key(name),
                         f"{path} @ {stamp} rows={report.total_rows}"),
                    )
            finally:
                _detach(conn)
    return reports
