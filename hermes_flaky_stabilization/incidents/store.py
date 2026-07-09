"""Local SQLite + FTS5 index of Jira incidents.

Profile-scoped, network-free, fast.  The store is the single source of
truth the provider serves ``prefetch()`` and the three tools from; ingest
(:mod:`ingest`) writes into it from Jira in the background.

The DB path is always derived from the caller-supplied ``hermes_home`` (or
an explicit path) — never hardcoded — so each Hermes profile keeps its own
index (plan §3.8).

Schema
------
``incidents``        one row per incident (full record + raw JSON)
``incidents_fts``    standalone FTS5 mirror of the searchable columns
``links``            session ↔ incident links recorded via the tool
``meta``             small key/value bag (e.g. the sync watermark)

A *standalone* (non-external-content) FTS5 table is used deliberately:
``incidents`` has a TEXT primary key (the incident key), which does not map
cleanly onto FTS5's integer ``content_rowid``.  Keeping a parallel FTS table
and re-writing its row on every upsert is simple and robust.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Columns persisted on the incidents table (besides the JSON blob/timestamps).
INCIDENT_COLUMNS = (
    "key",
    "summary",
    "status",
    "root_cause",
    "reporter",
    "assignee",
    "created",
    "updated",
    "body",
)

# Columns compared to decide whether an upsert actually changes a stored
# incident (everything bar the identity key). Used so a no-op re-ingest — which
# a steady-state incremental sync produces every cycle, because it always
# re-fetches the watermark-boundary issues — neither rewrites the FTS mirror nor
# invalidates the provider's prefetch cache.
_CONTENT_COLUMNS = tuple(c for c in INCIDENT_COLUMNS if c != "key")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class IncidentStore:
    """Thread-safe SQLite store for indexed Jira incidents."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        # The index holds the FULL, unredacted incident record (emails, names,
        # IPs, and any secret pasted into a ticket). It is sensitive data at
        # rest, so it must never be group/world-readable on a shared host.
        self._is_file_db = bool(self.db_path) and self.db_path != ":memory:"
        parent = os.path.dirname(self.db_path)
        if parent:
            # mode applies only when the directory is newly created; we do not
            # forcibly re-chmod a pre-existing (possibly shared) profile dir.
            os.makedirs(parent, mode=0o700, exist_ok=True)
        # Create the DB file owner-only *before* SQLite opens it, so incident
        # PII is never briefly exposed at the default umask.
        self._precreate_private()
        # check_same_thread=False because ingest/prefetch run on daemon
        # threads; all access is guarded by self._lock.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Cached row count, invalidated on every write that changes the
        # incident set. count() is hit once per turn by system_prompt_block(),
        # so memoising it avoids a table walk every turn.
        self._count_cache: int | None = None
        self._configure_connection()
        self._init_schema()
        # Enforce 0600 on the db and any sidecar files SQLite may have created.
        # Runs after _init_schema so the WAL/-shm files (created by the first
        # write transaction in WAL mode) are locked down too.
        self._harden_permissions()

    # -- connection tuning ---------------------------------------------------

    def _configure_connection(self) -> None:
        """Apply SQLite pragmas suited to a rebuildable local cache.

        WAL lets the background ingest writer and the read paths (prefetch /
        search / count) proceed without blocking each other; synchronous=NORMAL
        is safe under WAL and drops a per-commit fsync. Full durability is
        unnecessary here — the index is always reconstructable from Jira.
        """
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA temp_store=MEMORY")
        except sqlite3.Error as e:
            logger.debug("store: could not apply performance pragmas: %s", e)

    # -- at-rest hardening ---------------------------------------------------

    def _precreate_private(self) -> None:
        if not self._is_file_db or os.path.exists(self.db_path):
            return
        try:
            fd = os.open(self.db_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        except OSError:
            # Best-effort; _harden_permissions() chmods after connect too.
            pass

    def _harden_permissions(self) -> None:
        if not self._is_file_db:
            return
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = self.db_path + suffix
            try:
                if os.path.exists(path):
                    os.chmod(path, 0o600)
            except OSError:
                pass

    # -- schema --------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    key        TEXT PRIMARY KEY,
                    summary    TEXT,
                    status     TEXT,
                    root_cause TEXT,
                    reporter   TEXT,
                    assignee   TEXT,
                    created    TEXT,
                    updated    TEXT,
                    body       TEXT,
                    raw_json   TEXT,
                    indexed_at TEXT
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS incidents_fts USING fts5(
                    key, summary, status, root_cause, body,
                    tokenize = 'unicode61'
                );

                CREATE TABLE IF NOT EXISTS links (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id   TEXT,
                    incident_key TEXT,
                    note         TEXT,
                    created_at   TEXT
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                -- Supports the retention sweep's `updated < cutoff` range scan
                -- (prune_older_than), which otherwise walks the whole table.
                CREATE INDEX IF NOT EXISTS idx_incidents_updated
                    ON incidents(updated);
                """
            )
            self._conn.commit()

    # -- writes --------------------------------------------------------------

    _UPSERT_SQL = """
        INSERT INTO incidents
            (key, summary, status, root_cause, reporter, assignee,
             created, updated, body, raw_json, indexed_at)
        VALUES (:key, :summary, :status, :root_cause, :reporter,
                :assignee, :created, :updated, :body, :raw_json,
                :indexed_at)
        ON CONFLICT(key) DO UPDATE SET
            summary=excluded.summary,
            status=excluded.status,
            root_cause=excluded.root_cause,
            reporter=excluded.reporter,
            assignee=excluded.assignee,
            created=excluded.created,
            updated=excluded.updated,
            body=excluded.body,
            raw_json=excluded.raw_json,
            indexed_at=excluded.indexed_at
    """
    _FTS_INSERT_SQL = (
        "INSERT INTO incidents_fts (key, summary, status, root_cause, body) "
        "VALUES (:key, :summary, :status, :root_cause, :body)"
    )

    @staticmethod
    def _prepare_row(incident: dict[str, Any]) -> dict[str, Any]:
        """Build the full parameter row for one incident (no DB I/O).

        Unknown/missing fields default to empty strings. ``raw_json`` is taken
        from the incident if supplied; otherwise the record is serialised as a
        fallback. Raises ``ValueError`` when the required ``key`` is missing.
        """
        key = (incident.get("key") or "").strip()
        if not key:
            raise ValueError("incident is missing required 'key'")
        row = {col: incident.get(col, "") or "" for col in INCIDENT_COLUMNS}
        row["key"] = key
        raw = incident.get("raw_json")
        if raw is None:
            try:
                raw = json.dumps(incident, default=str)
            except Exception:
                raw = ""
        row["raw_json"] = raw
        row["indexed_at"] = _now_iso()
        return row

    def upsert(self, incident: dict[str, Any]) -> str:
        """Insert or replace one incident (keyed by ``key``). Returns the key.

        Unknown/missing fields default to empty strings.  The full original
        record is preserved in ``raw_json`` for later re-mapping.
        """
        row = self._prepare_row(incident)
        key = row["key"]
        with self._lock:
            self._conn.execute(self._UPSERT_SQL, row)
            # Refresh the FTS mirror row.
            self._conn.execute("DELETE FROM incidents_fts WHERE key = ?", (key,))
            self._conn.execute(self._FTS_INSERT_SQL, row)
            self._conn.commit()
            self._count_cache = None
        return key

    def upsert_many(self, incidents: list[dict[str, Any]]) -> int:
        """Insert/replace a batch of incidents in a single transaction.

        Returns the number of rows that were genuinely **new or changed**.
        Unchanged rows are skipped entirely — no write, no FTS rewrite — so a
        steady-state incremental sync (which always re-fetches the
        watermark-boundary issues) neither churns the index nor signals a change
        the provider would use to invalidate its prefetch cache. Committing once
        per page keeps the bulk load to a single fsync. Per-row mapping errors
        are skipped.
        """
        rows = []
        for inc in incidents:
            try:
                rows.append(self._prepare_row(inc))
            except Exception:
                continue
        if not rows:
            return 0
        with self._lock:
            changed = self._changed_rows(rows)
            if changed:
                keys = [(r["key"],) for r in changed]
                self._conn.executemany(self._UPSERT_SQL, changed)
                # Refresh the FTS mirror only for the rows that changed.
                self._conn.executemany("DELETE FROM incidents_fts WHERE key = ?", keys)
                self._conn.executemany(self._FTS_INSERT_SQL, changed)
                self._conn.commit()
                self._count_cache = None
        return len(changed)

    def _changed_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the subset of *rows* that are new or differ from what's stored.

        Caller must hold ``self._lock``. Reads the stored content columns for the
        incoming keys in chunked ``IN`` lookups (PK-indexed); a row counts as
        changed when its key is absent or any content column differs.
        """
        keys = [r["key"] for r in rows]
        existing: dict[str, sqlite3.Row] = {}
        col_list = ", ".join(_CONTENT_COLUMNS)
        chunk_size = 400  # stay well under SQLite's default 999-variable limit
        for i in range(0, len(keys), chunk_size):
            chunk = keys[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            cur = self._conn.execute(
                f"SELECT key, {col_list} FROM incidents WHERE key IN ({placeholders})",
                chunk,
            )
            for r in cur.fetchall():
                existing[r["key"]] = r
        changed = []
        for row in rows:
            old = existing.get(row["key"])
            if old is None or any(row[c] != old[c] for c in _CONTENT_COLUMNS):
                changed.append(row)
        return changed

    def record_link(self, session_id: str, incident_key: str, note: str = "") -> dict[str, Any]:
        """Record a link between a session and an incident. Returns the row."""
        created_at = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO links (session_id, incident_key, note, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id or "", incident_key or "", note or "", created_at),
            )
            self._conn.commit()
            link_id = cur.lastrowid
        return {
            "id": link_id,
            "session_id": session_id or "",
            "incident_key": incident_key or "",
            "note": note or "",
            "created_at": created_at,
        }

    # -- reads ---------------------------------------------------------------

    @staticmethod
    def _build_match(query: str) -> str | None:
        """Turn a free-text query into a safe FTS5 MATCH expression.

        Tokens are extracted as alphanumeric runs and quoted, which neutralises
        FTS5 operators in user input.  Returns ``None`` for an empty/blank or
        token-free query so the caller can short-circuit to ``[]``.

        A non-string *query* (a model may emit a number/array for the tool's
        string arg) is coerced to ``str`` rather than raising — this is the
        synchronous tool path, which must degrade gracefully, not break.
        """
        if query is None:
            return None
        if not isinstance(query, str):
            query = str(query)
        if not query.strip():
            return None
        tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
        tokens = [t for t in tokens if t]
        if not tokens:
            return None
        return " OR ".join('"' + t.replace('"', '') + '"' for t in tokens)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Full-text search over the index, best matches first.

        Empty/whitespace/token-free queries return ``[]`` (never an error).
        """
        match = self._build_match(query)
        if match is None:
            return []
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 5
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT i.key, i.summary, i.status, i.root_cause,
                           i.reporter, i.assignee, i.created, i.updated, i.body
                    FROM incidents_fts f
                    JOIN incidents i ON i.key = f.key
                    WHERE incidents_fts MATCH ?
                    ORDER BY bm25(incidents_fts)
                    LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
            except sqlite3.Error:
                # Malformed MATCH despite quoting, or a connection torn down
                # mid-flight (e.g. an in-flight prefetch racing shutdown's
                # close()) — degrade to no results, never raise on this path.
                return []
        return [dict(r) for r in rows]

    def get(self, incident_key: str) -> dict[str, Any] | None:
        """Return the full row for one incident key, or ``None``.

        Coerces a non-string *incident_key* (a model may emit a number for the
        tool's string arg) to ``str`` rather than raising — the tool path must
        degrade gracefully.
        """
        if incident_key is None:
            return None
        if not isinstance(incident_key, str):
            incident_key = str(incident_key)
        incident_key = incident_key.strip()
        if not incident_key:
            return None
        with self._lock:
            # raw_json is intentionally not selected: no read path consumes it,
            # and it can be a large blob (full issue payload). Skipping it keeps
            # the synchronous tool path (jira_get_root_cause) from reading and
            # redacting text it never surfaces.
            row = self._conn.execute(
                "SELECT key, summary, status, root_cause, reporter, assignee, "
                "created, updated, body, indexed_at "
                "FROM incidents WHERE key = ?",
                (incident_key,),
            ).fetchone()
        return dict(row) if row else None

    def links_for(self, incident_key: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, session_id, incident_key, note, created_at "
                "FROM links WHERE incident_key = ? ORDER BY id",
                (incident_key or "",),
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            if self._count_cache is None:
                self._count_cache = int(
                    self._conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
                )
            return self._count_cache

    def prune_older_than(self, cutoff_date: str) -> int:
        """Delete incidents whose ``updated`` date is before ``cutoff_date``.

        ``cutoff_date`` is a ``YYYY-MM-DD`` string; comparison uses the
        leading 10 chars of ``updated`` (ISO / Jira timestamps share that
        prefix). Rows with an empty/unparseable ``updated`` are kept.
        Returns the number of incidents removed.
        """
        if not cutoff_date:
            return 0
        # `updated < cutoff` is equivalent to comparing the leading 10 chars
        # (ISO/Jira timestamps share the YYYY-MM-DD prefix) but, unlike a
        # substr() expression, it is sargable against idx_incidents_updated.
        # The `updated != ''` / length guards keep rows with no parseable
        # timestamp, matching the previous behaviour.
        predicate = "updated != '' AND length(updated) >= 10 AND updated < ?"
        with self._lock:
            # Delete the FTS mirror rows first (via subquery, while the source
            # rows still exist), then the incidents themselves.
            self._conn.execute(
                f"DELETE FROM incidents_fts WHERE key IN "
                f"(SELECT key FROM incidents WHERE {predicate})",
                (cutoff_date,),
            )
            cur = self._conn.execute(
                f"DELETE FROM incidents WHERE {predicate}", (cutoff_date,)
            )
            removed = cur.rowcount or 0
            self._conn.commit()
            if removed:
                self._count_cache = None
        return removed

    # -- watermark / meta ----------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_watermark(self) -> str | None:
        """Most recent ``updated`` timestamp ingested, used for incremental sync."""
        return self.get_meta("sync_watermark")

    def set_watermark(self, value: str) -> None:
        if value:
            self.set_meta("sync_watermark", value)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n_links = int(self._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])
        return {
            "db_path": self.db_path,
            "incidents": self.count(),
            "links": n_links,
            "watermark": self.get_watermark(),
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
