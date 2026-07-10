"""Read-only reader of the ``hermes-test-history`` SQLite database.

This is the only place that touches another plugin's data file. It opens the DB
**read-only** (``mode=ro`` URI), reads one logical run per
``(source_file, run_timestamp)`` to undo test-history's lack of ingest dedup
(§4), excludes ``skipped`` cases, and hands the pure detection core a list of
``(classname, name, file_path, status, eff_ts)`` tuples with each ``eff_ts``
normalized to a canonical naive-UTC ISO-8601 seconds string.

GPL/data-only boundary: we read the *data file* only and never import any
test-history Python module. The shared SQL fragments (the effective-timestamp
``COALESCE`` and the status literals) are this plugin's own ``domain`` constants.

Hard rules honored: the DB is opened read-only; every value binds through ``?``
(the only concatenated SQL is static fragments from ``domain``, never input); the
window comparison is wrapped in ``datetime()`` so test-history's ``T``-separated
``run_timestamp`` and space-separated ``ingested_at`` compare consistently.
"""

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import domain, timeutil

logger = logging.getLogger(__name__)


class TestHistoryUnavailable(Exception):
    """The test-history database is missing or cannot be opened for reading.

    Carries a human-actionable message (no stack trace) for the CLI/tool to
    surface directly.
    """


@dataclass
class ReadResult:
    """What a read of the test-history DB yields."""

    rows: list[tuple]                 # (classname, name, file_path, status, eff_ts)
    source_schema_version: int | None  # observed test-history schema_version, or None


# ---------------------------------------------------------------------------
# SQL (static fragments only — every value still binds through ``?``)
# ---------------------------------------------------------------------------

_ET = domain.effective_time()            # "COALESCE(run_timestamp, ingested_at)"
_SKIP = domain.STATUS_SKIPPED            # fixed internal constant, not input

# Window-function path: pick one logical run per (source_file, eff_ts), keeping
# the most recently ingested duplicate, then fetch its non-skipped cases.
_WINDOW_SQL = (
    "WITH logical_runs AS ("
    "  SELECT id, source_file, " + _ET + " AS eff_ts, "
    "         ROW_NUMBER() OVER ("
    "           PARTITION BY source_file, " + _ET + " "
    "           ORDER BY ingested_at DESC, id DESC"
    "         ) AS rn"
    "  FROM test_runs"
    "  WHERE datetime(" + _ET + ") >= datetime(?)"
    ") "
    "SELECT c.classname, c.name, c.file_path, c.status, lr.eff_ts "
    "FROM test_cases c "
    "JOIN logical_runs lr ON lr.id = c.run_id AND lr.rn = 1 "
    "WHERE c.status <> '" + _SKIP + "' "
    # Deterministic row order (by effective time, then insertion order) so the
    # core's output order — and which row wins the file_path backfill when a test's
    # path varies across runs — is stable, not left to SQLite's join order.
    "ORDER BY lr.eff_ts ASC, c.id ASC"
)

# Fallback runs query (no window function): ordered so the first row seen for a
# (source_file, eff_ts) key is the surviving (most recently ingested) duplicate.
_FALLBACK_RUNS_SQL = (
    "SELECT id, source_file, " + _ET + " AS eff_ts "
    "FROM test_runs "
    "WHERE datetime(" + _ET + ") >= datetime(?) "
    "ORDER BY ingested_at DESC, id DESC"
)
_FALLBACK_CASES_SQL = (
    "SELECT c.classname, c.name, c.file_path, c.status, c.run_id "
    "FROM test_cases c JOIN test_runs tr ON tr.id = c.run_id "
    "WHERE c.status <> '" + _SKIP + "' "
    "  AND datetime(" + domain.effective_time("tr.") + ") >= datetime(?) "
    "ORDER BY c.id ASC"
)


# ---------------------------------------------------------------------------
# Read-only connection
# ---------------------------------------------------------------------------


def read_only_uri(db_path) -> str:
    """The ``mode=ro`` connection URI for ``db_path`` (read-only, never writable).

    The path is percent-encoded (SQLite decodes percent-escapes in URI
    filenames): a raw ``#``, ``?``, or ``%`` would otherwise terminate or alter
    the URI's path part, silently dropping ``mode=ro`` and opening — or even
    creating — a *different* file read-write.
    """
    return f"file:{quote(str(Path(db_path)), safe='/')}?mode=ro"


def read_source_schema_version(conn: sqlite3.Connection) -> int | None:
    """Highest ``schema_version`` recorded by the test-history DB, or ``None``.

    ``None`` means the version is unreadable — the table is absent (the file may
    predate versioning or not be a test-history DB), or the file is not a valid
    SQLite database at all. We catch ``DatabaseError`` (the parent of
    ``OperationalError``) so a corrupt file reports ``None`` here rather than
    escaping; the windowed read that follows then converts it to a clean
    :class:`TestHistoryUnavailable`.
    """
    try:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row[0]) if row and row[0] is not None else 0


def _warn_on_schema_mismatch(version: int | None) -> None:
    """Warn (via logging, which reaches stderr by default) on a version we did
    not build against — but never fail: reading is best-effort (§3.1)."""
    expected = domain.EXPECTED_SOURCE_SCHEMA_VERSION
    if version is None:
        logger.warning(
            "test-history database has no schema_version row (expected %s); "
            "reading anyway, results may be unreliable.", expected,
        )
    elif version != expected:
        logger.warning(
            "test-history schema_version is %s but this plugin was built for %s; "
            "reading anyway, results may be unreliable.", version, expected,
        )


def _warn_on_undedupable_runs(conn: sqlite3.Connection, cutoff: str) -> None:
    """Warn when reingest dedup cannot collapse some in-window runs (best-effort).

    Dedup keys on ``(source_file, COALESCE(run_timestamp, ingested_at))``. When a
    run has **no ``run_timestamp``** the key degrades to ``ingested_at``, which is
    unique per ingest — so re-ingesting the same timestamp-less artifact is *not*
    collapsed and its cases are counted once per ingest, which can inflate a test's
    fail count past ``min_fails`` and misclassify it. We cannot distinguish a
    re-ingest from a genuinely distinct run without a ``run_timestamp``, so this is
    surfaced as a caution (never a failure): if any in-window ``source_file`` has
    more than one run lacking a ``run_timestamp``, warn so the operator can re-ingest
    with a timestamp or prune duplicates. Wrapped in a tolerant ``try`` so a
    malformed source DB still reaches the clean error raised by the windowed read.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT 1 FROM test_runs"
            "  WHERE run_timestamp IS NULL AND datetime(ingested_at) >= datetime(?)"
            "  GROUP BY source_file HAVING COUNT(*) > 1"
            ")",
            (cutoff,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return
    affected = int(row[0]) if row and row[0] is not None else 0
    if affected:
        logger.warning(
            "%s source file(s) have multiple in-window runs with no run_timestamp; "
            "reingest dedup keys on run_timestamp and cannot collapse these, so their "
            "tests' pass/fail counts may be inflated by duplicate ingests. Re-ingest "
            "with a run timestamp, or prune the duplicate runs in hermes-test-history.",
            affected,
        )


def _normalize_rows(raw_rows) -> list[tuple]:
    """Canonicalize each row's eff_ts so the pure core can compare as strings.

    All cases of one logical run share a single eff_ts, so the raw value repeats
    far more often than it varies (a busy window has thousands of cases but only
    a handful of distinct timestamps). Memoize the normalization per call — a
    *local* cache, not a module-level one, so it cannot grow unbounded in the
    long-lived gateway process.
    """
    out: list[tuple] = []
    cache: dict = {}
    for classname, name, file_path, status, eff_ts in raw_rows:
        if eff_ts in cache:
            ts = cache[eff_ts]
        else:
            ts = timeutil.normalize_ts(eff_ts)
            if ts is None:
                # Unparseable or non-string eff_ts (e.g. a numeric/epoch timestamp
                # from a malformed source DB): keep a *string* form so the pure core
                # only ever compares str-to-str and never raises a TypeError on a
                # mixed-type row. eff_ts is never NULL (it is a COALESCE of two
                # columns, one NOT NULL), but guard it anyway.
                ts = str(eff_ts) if eff_ts is not None else None
            cache[eff_ts] = ts
        out.append((classname, name, file_path, status, ts))
    return out


def _fallback_read(conn: sqlite3.Connection, cutoff: str) -> list[tuple]:
    """Two-step Python dedup for SQLite builds without window functions.

    Equivalent to ``_WINDOW_SQL``: keep one logical run per
    ``(source_file, eff_ts)`` (the most recently ingested), then attach that run's
    eff_ts to its non-skipped cases. Extracted as its own function so the suite
    can assert it matches the window-function path exactly.
    """
    survivors: dict[tuple, int] = {}     # (source_file, eff_ts) -> surviving run_id
    eff_by_run: dict[int, str] = {}
    for rid, source_file, eff_ts in conn.execute(_FALLBACK_RUNS_SQL, (cutoff,)):
        key = (source_file, eff_ts)
        if key not in survivors:          # first row per key wins (ORDER BY above)
            survivors[key] = rid
            eff_by_run[rid] = eff_ts
    survivor_ids = set(survivors.values())

    raw: list[tuple] = []
    for classname, name, file_path, status, run_id in conn.execute(
        _FALLBACK_CASES_SQL, (cutoff,)
    ):
        if run_id in survivor_ids:
            raw.append((classname, name, file_path, status, eff_by_run[run_id]))
    # Normalize eff_ts BEFORE sorting: a numeric run_timestamp (from a malformed
    # source DB) would otherwise make the sort key compare int/float to str and
    # raise TypeError — the window-function path normalizes too, so both paths
    # stay equivalent. Then match its ORDER BY (eff_ts, then case insertion
    # order): the cases query already yields rows in c.id order; a stable sort on
    # eff_ts promotes that to (eff_ts ASC, c.id ASC).
    raw = _normalize_rows(raw)
    raw.sort(key=lambda r: r[4])
    return raw


def _read_windowed(conn: sqlite3.Connection, cutoff: str) -> list[tuple]:
    """Dedup + windowed case rows, preferring the SQL window-function path.

    Falls back to :func:`_fallback_read` if the SQLite build lacks window
    functions (very old builds) — the result is identical.
    """
    try:
        cur = conn.execute(_WINDOW_SQL, (cutoff,))
        return _normalize_rows(cur.fetchall())
    except sqlite3.OperationalError as exc:
        # Window functions need SQLite >= 3.25; older builds raise a syntax error.
        if "OVER" not in str(exc) and "window" not in str(exc).lower():
            raise
        logger.warning("SQLite lacks window functions; using the fallback dedup path.")
        return _fallback_read(conn, cutoff)


def read_test_history(db_path, cutoff: str) -> ReadResult:
    """Open ``db_path`` read-only and return windowed, deduped, normalized rows.

    Raises :class:`TestHistoryUnavailable` (with an actionable message, no
    traceback) when the database file does not exist or cannot be opened.
    """
    path = Path(db_path)
    if not path.exists():
        raise TestHistoryUnavailable(
            f"test-history database not found at {path}. Install and populate the "
            "hermes-test-history plugin (`hermes test-history ingest <path>`), or set "
            "`test_history_db_path` in the detective config section."
        )
    try:
        conn = sqlite3.connect(read_only_uri(path), uri=True)
    except sqlite3.OperationalError as exc:
        raise TestHistoryUnavailable(
            f"could not open the test-history database at {path} read-only: {exc}"
        ) from exc
    try:
        conn.row_factory = sqlite3.Row
        version = read_source_schema_version(conn)
        _warn_on_schema_mismatch(version)
        _warn_on_undedupable_runs(conn, cutoff)
        try:
            rows = _read_windowed(conn, cutoff)
        except sqlite3.DatabaseError as exc:
            # e.g. "no such table: test_runs" (OperationalError — exists but wrong
            # schema) or "file is not a database" (DatabaseError — a corrupt/non-
            # SQLite file). DatabaseError is the parent of OperationalError, so this
            # one catch covers both. Surface either as a clean, actionable error.
            raise TestHistoryUnavailable(
                f"the file at {path} does not look like a test-history database: {exc}"
            ) from exc
        return ReadResult(rows=rows, source_schema_version=version)
    finally:
        conn.close()
