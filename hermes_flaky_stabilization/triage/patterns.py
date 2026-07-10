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

Pure standard library (``sqlite3``, ``hashlib``, ``re``, ``datetime``); the
caller supplies the DB path so the module stays decoupled from Hermes and
unit-testable.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_RETENTION_DAYS = 180
DEFAULT_MAX_ROWS_PER_PROJECT = 500
_SAMPLE_CHARS = 4000   # bounded excerpt stored per pattern for fuzzy lookup

# --------------------------------------------------------------------------
# Signature normalisation
# --------------------------------------------------------------------------

# ORDER MATTERS: rules run top-to-bottom, so specific patterns must precede
# general ones (timestamps before clock times; long hex blobs before the
# bare-integer catch-all). Reordering can let a greedy rule mask a precise one.
_NORMALISERS = [
    # ISO-8601-ish timestamps: 2026-05-31T17:04:01.123Z / 2026-05-31 17:04:01
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    # clock times: 17:04:01 / 17:04:01.123
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<TS>"),
    # UUIDs
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    # hex addresses: 0xdeadbeef
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<ADDR>"),
    # long hex blobs (git sha, content hashes) — 7+ hex chars
    (re.compile(r"\b[0-9a-fA-F]{7,}\b"), "<HEX>"),
    # temp paths
    (re.compile(r"(?:/tmp|/var/folders|/var/tmp|/private/var/folders)/[^\s:'\"]+"), "<TMP>"),
    # explicit line/offset markers: 'line 1234', 'offset 42', ':123:45'
    (re.compile(r"\b(?:line|offset|byte)\s+\d+\b", re.IGNORECASE), r"<POS>"),
    (re.compile(r":\d+:\d+\b"), ":<POS>"),
    (re.compile(r":\d+\b"), ":<POS>"),
    # any remaining bare integer run
    (re.compile(r"\b\d+\b"), "<N>"),
]


def normalize_signature_text(text: str) -> str:
    """Normalise volatile tokens so equivalent failures share a signature."""
    out = text or ""
    for pattern, repl in _NORMALISERS:
        out = pattern.sub(repl, out)
    # collapse whitespace so reflowed/indented variants converge
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()


def compute_signature(excerpt: str) -> str | None:
    """Return a stable SHA-1 hex signature for a (pre-filtered) excerpt.

    Returns ``None`` for a *degenerate* excerpt — one whose volatile tokens
    normalise away to nothing (blank/whitespace-only, or pure timestamps/IDs).
    Such an excerpt hashes to the empty-string signature, which would collide
    across every such log, so it must not be stored or looked up. Returning
    ``None`` keeps that rule inside the store instead of forcing the caller to
    re-derive it by testing the normalised text for emptiness.
    """
    normalised = normalize_signature_text(excerpt)
    if not normalised:
        return None
    # Non-cryptographic content fingerprint (dedup key), hence usedforsecurity=False.
    return hashlib.sha1(
        normalised.encode("utf-8", "replace"), usedforsecurity=False
    ).hexdigest()


def _iso_now(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat()


def fts5_available() -> bool:
    """Probe whether the bundled sqlite3 supports FTS5 (no side effects)."""
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(c)")
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


_FTS_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

# Ubiquitous failure vocabulary — present in virtually every CI log, so a fuzzy
# match on one of these alone is meaningless (it would surface the project's
# most-seen pattern as a "similar" prior for any unrelated failure). Excluded
# from the fuzzy query and from the overlap count.
_FUZZY_STOPWORDS = frozenset({
    "error", "errors", "errored",
    "fail", "fails", "failed", "failing", "failure", "failures",
    "exception", "exceptions",
})

# How many fuzzy candidates to inspect for genuine token overlap.
_FUZZY_CANDIDATES = 10


def _fuzzy_tokens(excerpt: str, limit: int = 12) -> list[str]:
    """Pick salient alphanumeric tokens from an excerpt for an FTS query.

    Stopwords (ubiquitous failure vocabulary) are excluded: they carry no
    similarity signal and would let any log fuzzy-match any pattern.
    """
    seen: dict[str, None] = {}
    for tok in _FTS_TOKEN_RE.findall(normalize_signature_text(excerpt)):
        low = tok.lower()
        if low in _FUZZY_STOPWORDS:
            continue
        if low not in seen:
            seen[low] = None
        if len(seen) >= limit:
            break
    return list(seen)


def _sample_tokens(sample: str) -> set[str]:
    """The stored sample's token set, derived the same way as the query tokens."""
    return {t.lower() for t in _FTS_TOKEN_RE.findall(normalize_signature_text(sample or ""))}


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
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.fts = fts5_available() if fts is None else bool(fts)
        self._init_schema()

    # -- schema ------------------------------------------------------------

    def _init_schema(self) -> None:
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS triage_patterns (
                project     TEXT NOT NULL,
                signature   TEXT NOT NULL,
                category    TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen  TEXT NOT NULL,
                last_seen   TEXT NOT NULL,
                sample      TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project, signature)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_triage_patterns_last_seen "
            "ON triage_patterns(project, last_seen)"
        )
        if self.fts:
            try:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS triage_patterns_fts USING fts5("
                    "sample, project UNINDEXED, signature UNINDEXED)"
                )
                # Backfill rows missing from the index — e.g. a DB previously
                # written with FTS disabled, or created before this table existed.
                # Without this, those priors stay invisible to fuzzy lookup.
                self.conn.execute(
                    "INSERT INTO triage_patterns_fts (sample, project, signature) "
                    "SELECT sample, project, signature FROM triage_patterns p "
                    "WHERE NOT EXISTS (SELECT 1 FROM triage_patterns_fts f "
                    "WHERE f.project = p.project AND f.signature = p.signature)"
                )
            except sqlite3.Error:
                # FTS5 vanished between probe and create — degrade gracefully.
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
        tokens = _fuzzy_tokens(excerpt)
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
                    (project, match, _FUZZY_CANDIDATES),
                ).fetchall()
            else:
                clauses = " OR ".join("sample LIKE ?" for _ in tokens)
                params = [project] + [f"%{t}%" for t in tokens] + [_FUZZY_CANDIDATES]
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
                if len(_sample_tokens(row["sample"]) & set(tokens)) >= min_overlap:
                    return self._row_to_dict(row, fuzzy=True)
            return None
        # LIKE fallback has no relevance ranking — approximate it: most shared
        # tokens first, occurrences as the tie-break.
        best = None
        best_key = (0, 0)
        for row in rows:
            overlap = len(_sample_tokens(row["sample"]) & set(tokens))
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
