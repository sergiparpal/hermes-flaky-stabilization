"""Per-project failure-pattern store (unified state.db).

Unified-plugin adaptation: tables renamed ``patterns`` -> ``triage_patterns``
and ``patterns_fts`` -> ``triage_patterns_fts`` (plan Appendix A); everything
else is verbatim from hermes-ci-triage. The caller now points ``db_path`` at
the consolidated ``state.db``, whose migration step 1 pre-creates the same
DDL (the CREATEs below stay as idempotent no-ops).

``general`` plugins have no Curator to manage retention for them, so this
module owns its own lifecycle: it learns failure *signatures* per project,
serves prior classifications back as a hint, and prunes itself (age + per-
project row cap) on every write.

A **signature** is a SHA-1 of the excerpt after volatile tokens (timestamps,
hex addresses, UUIDs, line/byte offsets, temp paths, bare numbers) are
normalised away, so two runs of the same failure that differ only in those
tokens collapse to one signature.

FTS5 is used for fuzzy lookup when available and falls back to ``LIKE``
otherwise — detected at open time, never assumed.

The base-table schema (``triage_patterns`` + its FTS mirror) and the migration
ladder are owned by :mod:`hermes_flaky_stabilization.storage.state`, the single
schema owner for ``state.db``; this module used to re-declare that DDL, which
meant a future ``state`` migration would never reach a DB opened here. It now
delegates schema creation to ``state.ensure_schema``. Hermes-free at import
time (``state`` reaches the host only lazily), so the store stays unit-testable
with any DB path the caller supplies.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from ..storage import state
from .signatures import (
    FUZZY_CANDIDATES,
    fts5_available,
    fuzzy_tokens,
    sample_tokens,
)
from .signatures import (
    compute_signature as compute_signature,  # re-exported: patterns.compute_signature
)

DEFAULT_RETENTION_DAYS = 180
DEFAULT_MAX_ROWS_PER_PROJECT = 500
_SAMPLE_CHARS = 4000   # bounded excerpt stored per pattern for fuzzy lookup


def _iso_now(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat()


class PatternStore:
    """SQLite-backed per-project pattern store with self-retention.

    Open one per operation and :meth:`close` it; SQLite is opened in WAL mode
    for concurrent readers. Set ``fts=False`` to force the ``LIKE`` path
    (used by tests to exercise the fallback even where FTS5 is present).
    """

    def __init__(
        self,
        db_path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_rows_per_project: int = DEFAULT_MAX_ROWS_PER_PROJECT,
        fts: bool | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.retention_days = retention_days
        self.max_rows_per_project = max_rows_per_project
        # The shared private-WAL recipe owns precreate-0600 + connect + WAL (this
        # store used to open a plain, unhardened connection of its own).
        from ..storage.db import open_private

        self.conn = open_private(self.db_path)
        self.fts = fts5_available() if fts is None else bool(fts)
        self._init_schema()

    # -- schema ------------------------------------------------------------

    def _init_schema(self) -> None:
        # WAL is applied by the shared connection recipe (open_private).
        # Base tables (``triage_patterns`` + index + the FTS mirror) and the
        # migration ladder are owned by the shared state layer; the DDL used to
        # be duplicated here, so a future ``state`` migration never reached a DB
        # opened through this store. ``ensure_schema`` is idempotent and records
        # ``schema_version`` on this connection so later migrations apply.
        state.ensure_schema(self.conn)
        if self.fts:
            try:
                # The table itself is created by ``ensure_schema``; this
                # backfills rows missing from the index — e.g. a DB previously
                # written with FTS disabled — which otherwise stay invisible to
                # fuzzy lookup. A raise here means FTS5 vanished between the
                # open-time probe and now; degrade to the LIKE path.
                self.conn.execute(
                    "INSERT INTO triage_patterns_fts (sample, project, signature) "
                    "SELECT sample, project, signature FROM triage_patterns p "
                    "WHERE NOT EXISTS (SELECT 1 FROM triage_patterns_fts f "
                    "WHERE f.project = p.project AND f.signature = p.signature)"
                )
            except sqlite3.Error:
                self.fts = False
        self.conn.commit()

    # -- reads -------------------------------------------------------------

    def _fetch_row(self, project: str, signature: str) -> sqlite3.Row | None:
        """The canonical single-pattern read, shared by lookup and record."""
        return self.conn.execute(
            "SELECT * FROM triage_patterns WHERE project=? AND signature=?",
            (project, signature),
        ).fetchone()

    def lookup(
        self, project: str, signature: str, excerpt: str | None = None
    ) -> dict[str, object] | None:
        """Return the prior record for a signature, or a fuzzy match, else None.

        Exact ``(project, signature)`` match wins. On a miss, if an *excerpt*
        is supplied a fuzzy lookup is attempted (FTS5 when available, ``LIKE``
        otherwise) and the most-seen similar prior is returned with
        ``fuzzy=True``.
        """
        row = self._fetch_row(project, signature)
        if row is not None:
            return self._row_to_dict(row, fuzzy=False)
        if excerpt:
            fuzzy = self._fuzzy_lookup(project, excerpt)
            if fuzzy is not None:
                return fuzzy
        return None

    def _fuzzy_lookup(
        self, project: str, excerpt: str
    ) -> dict[str, object] | None:
        tokens = fuzzy_tokens(excerpt)
        if not tokens:
            return None
        # A "similar" prior must share real signal with the excerpt: at least
        # two distinct non-stopword tokens (or the whole query when it has only
        # one). Ranking by occurrences alone let one shared common token return
        # the project's most-seen pattern for any unrelated failure.
        min_overlap = min(2, len(tokens))
        try:
            if self.fts:
                match = " OR ".join(f'"{t}"' for t in tokens)
                rows = self.conn.execute(
                    "SELECT p.* FROM triage_patterns_fts f "
                    "JOIN triage_patterns p ON p.project=f.project AND p.signature=f.signature "
                    "WHERE f.project=? AND triage_patterns_fts MATCH ? "
                    "ORDER BY bm25(triage_patterns_fts) LIMIT ?",
                    (project, match, FUZZY_CANDIDATES),
                ).fetchall()
            else:
                clauses = " OR ".join("sample LIKE ?" for _ in tokens)
                params = [project] + [f"%{t}%" for t in tokens] + [FUZZY_CANDIDATES]
                rows = self.conn.execute(
                    f"SELECT * FROM triage_patterns WHERE project=? AND ({clauses}) "
                    "ORDER BY occurrences DESC LIMIT ?",
                    params,
                ).fetchall()
        except sqlite3.Error:
            return None
        if self.fts:
            # Candidates arrive best-relevance-first (bm25); take the first
            # with enough genuine overlap.
            for row in rows:
                if len(sample_tokens(row["sample"]) & set(tokens)) >= min_overlap:
                    return self._row_to_dict(row, fuzzy=True)
            return None
        # LIKE fallback has no relevance ranking — approximate it: most shared
        # tokens first, occurrences as the tie-break.
        best = None
        best_key = (0, 0)
        for row in rows:
            overlap = len(sample_tokens(row["sample"]) & set(tokens))
            if overlap < min_overlap:
                continue
            key = (overlap, row["occurrences"])
            if best is None or key > best_key:
                best, best_key = row, key
        if best is None:
            return None
        return self._row_to_dict(best, fuzzy=True)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, *, fuzzy: bool) -> dict[str, object]:
        return {
            "project": row["project"],
            "signature": row["signature"],
            "category": row["category"],
            "occurrences": row["occurrences"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "sample": row["sample"],
            "fuzzy": fuzzy,
        }

    # -- writes ------------------------------------------------------------

    def record(
        self,
        project: str,
        signature: str,
        category: str,
        excerpt: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Upsert a pattern: bump ``occurrences`` and ``last_seen``.

        Stores a bounded sample of the excerpt for fuzzy lookup. Runs
        retention (age prune + per-project cap) after the write.
        """
        ts = _iso_now(now)
        sample = (excerpt or "")[:_SAMPLE_CHARS]
        existing = self.conn.execute(
            "SELECT occurrences FROM triage_patterns WHERE project=? AND signature=?",
            (project, signature),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO triage_patterns "
                "(project, signature, category, occurrences, first_seen, last_seen, sample) "
                "VALUES (?, ?, ?, 1, ?, ?, ?)",
                (project, signature, category, ts, ts, sample),
            )
        else:
            self.conn.execute(
                "UPDATE triage_patterns SET occurrences=occurrences+1, last_seen=?, "
                "category=?, sample=? WHERE project=? AND signature=?",
                (ts, category, sample, project, signature),
            )
        self._fts_delete(project, signature)
        self._fts_insert(project, signature, sample)
        self.conn.commit()
        self.prune(project, now=now)
        row = self._fetch_row(project, signature)
        return self._row_to_dict(row, fuzzy=False) if row else {}

    # -- retention ---------------------------------------------------------

    def prune(self, project: str | None = None, *, now: datetime | None = None) -> int:
        """Delete rows older than ``retention_days`` and enforce the per-
        project row cap (evicting least-recently-seen). Returns rows removed.
        """
        now_dt = now or datetime.now(UTC)
        cutoff = (now_dt - timedelta(days=self.retention_days)).isoformat()
        removed = 0

        stale = self.conn.execute(
            "SELECT project, signature FROM triage_patterns WHERE last_seen < ?"
            + (" AND project=?" if project else ""),
            (cutoff, project) if project else (cutoff,),
        ).fetchall()
        for r in stale:
            self._delete(r["project"], r["signature"])
            removed += 1

        projects = (
            [project]
            if project
            else [r["project"] for r in self.conn.execute(
                "SELECT DISTINCT project FROM triage_patterns").fetchall()]
        )
        for proj in projects:
            count = self.conn.execute(
                "SELECT COUNT(*) AS c FROM triage_patterns WHERE project=?", (proj,)
            ).fetchone()["c"]
            overflow = count - self.max_rows_per_project
            if overflow > 0:
                victims = self.conn.execute(
                    "SELECT signature FROM triage_patterns WHERE project=? "
                    "ORDER BY last_seen ASC LIMIT ?",
                    (proj, overflow),
                ).fetchall()
                for v in victims:
                    self._delete(proj, v["signature"])
                    removed += 1

        if removed:
            self.conn.commit()
        return removed

    def _delete(self, project: str, signature: str) -> None:
        self.conn.execute(
            "DELETE FROM triage_patterns WHERE project=? AND signature=?",
            (project, signature),
        )
        self._fts_delete(project, signature)

    # -- FTS index maintenance (best-effort; no-ops when FTS is off) --------

    def _fts_delete(self, project: str, signature: str) -> None:
        if not self.fts:
            return
        try:
            self.conn.execute(
                "DELETE FROM triage_patterns_fts WHERE project=? AND signature=?",
                (project, signature),
            )
        except sqlite3.Error:
            pass

    def _fts_insert(self, project: str, signature: str, sample: str) -> None:
        if not self.fts:
            return
        try:
            self.conn.execute(
                "INSERT INTO triage_patterns_fts (sample, project, signature) "
                "VALUES (?, ?, ?)",
                (sample, project, signature),
            )
        except sqlite3.Error:
            pass

    def count(self, project: str | None = None) -> int:
        if project:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM triage_patterns WHERE project=?", (project,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM triage_patterns").fetchone()
        return int(row["c"])

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> PatternStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
