"""Query functions backing the two LLM tools.

Each returns a plain dict; the tool handlers in ``__init__.py`` wrap them in the
``json.dumps`` success/error envelope. Hard rules honored here:

* every SQL statement is parameterized with ``?`` — no values are ever
  interpolated into SQL strings (hard-constraint #7);
* every input field from the LLM is validated before use (hard-constraint #9);
* output is bounded — at most ``limit`` failures, stack traces truncated to
  ``max_stack_trace_chars`` (catalog §6.2 "output bounded");
* a malformed FTS query (e.g. an injection attempt) yields empty results, not
  an exception.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

from . import domain
from .timeutil import parse_iso_window

# ---------------------------------------------------------------------------
# Limits / SQL fragments
# ---------------------------------------------------------------------------

_MAX_INPUT_LEN = 500          # cap on test_id / path length
_MAX_OFFENDERS = 50           # cap on module_failure_history rows (bound output)

# "What counts as a non-passing outcome", and its SQL list form, live in
# ``domain`` (shared with the test fixtures): ``domain.FAIL_STATUSES`` backs the
# Python-side filtering in ``test_failure_lookup`` and ``domain.FAIL_STATUSES_SQL``
# the grouped query below, so the two agree by construction. The SQL form is
# concatenated in as a *static fragment* (like ``_CASE_COLS`` — never a value).

# Shared column list, concatenated (not f-string interpolated) into the two
# case-fetching statements below.
_CASE_COLS = (
    "tc.id AS cid, tc.run_id, tc.classname, tc.name, tc.file_path, tc.status, "
    "tc.failure_message, tc.failure_type, tc.stack_trace, "
    "tr.run_timestamp, tr.ingested_at"
)
_EXACT_SELECT = (
    "SELECT " + _CASE_COLS + " FROM test_cases tc "
    "JOIN test_runs tr ON tr.id = tc.run_id WHERE "
)
_FTS_SELECT = (
    "SELECT " + _CASE_COLS + " FROM test_cases_fts f "
    "JOIN test_cases tc ON tc.id = f.rowid "
    "JOIN test_runs tr ON tr.id = tc.run_id "
    "WHERE test_cases_fts MATCH ?"
)


# ---------------------------------------------------------------------------
# Input validation / coercion
# ---------------------------------------------------------------------------


def _validate_str(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"`{field}` must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"`{field}` must not be empty")
    if len(cleaned) > _MAX_INPUT_LEN:
        raise ValueError(f"`{field}` too long (max {_MAX_INPUT_LEN} characters)")
    return cleaned


def _validate_path(path) -> str:
    cleaned = _validate_str(path, "path")
    if ".." in cleaned:
        # It is only a LIKE-prefix, never a filesystem read, but reject traversal
        # patterns anyway so the surface stays obviously safe.
        raise ValueError("`path` must not contain '..'")
    return cleaned


def _coerce_int(value, *, default: int, lo: int, hi: int | None) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + " …[truncated]"


def _like_prefix(prefix: str) -> str:
    """Build a LIKE pattern matching ``prefix*`` with wildcards escaped.

    The accompanying SQL uses ``ESCAPE '\\'`` so a literal ``%`` or ``_`` in the
    caller's path is matched literally rather than as a wildcard.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


# ---------------------------------------------------------------------------
# Tool: test_failure_lookup
# ---------------------------------------------------------------------------


def _row_timestamp(row) -> str:
    """A row's effective timestamp: ``run_timestamp``, else ``ingested_at``, else ''."""
    return row["run_timestamp"] or row["ingested_at"] or ""


def _row_timestamp_key(row) -> datetime:
    """Chronological sort key for a result row.

    Sort chronologically, not lexicographically: ``run_timestamp`` is
    ``T``-separated while ``ingested_at`` is space-separated, so a string sort
    would mis-order them on same-date ties, and any stray non-ISO value would
    sort to the top. Unparseable timestamps sort last.
    """
    try:
        return datetime.fromisoformat(_row_timestamp(row).replace(" ", "T", 1))
    except ValueError:
        return datetime.min


def _collect_fts(conn, match_query: str, into: dict) -> None:
    """Best-effort FTS lookup; malformed FTS syntax is swallowed (-> no rows)."""
    try:
        for row in conn.execute(_FTS_SELECT, (match_query,)):
            into.setdefault(row["cid"], row)
    except sqlite3.OperationalError:
        # e.g. "fts5: syntax error" from an injection-y / malformed query.
        pass


def _resolve_rows(conn, test_id: str) -> dict:
    """Resolve candidate case rows for ``test_id``, keyed by case id.

    Resolution order:
      * ``classname::name`` or ``file_path::name`` → exact structured match,
        falling back to FTS on the name part if nothing matched;
      * otherwise → exact name match, falling back to FTS if nothing matched.

    Exact matching is always attempted first and is safe for any input — the
    value only ever flows through a ``?`` placeholder — so a test whose literal
    name contains an FTS metacharacter (``*``, ``"``, ``^``, ``AND`` …) is still
    found by id. An explicit FTS query such as ``login AND oauth`` simply doesn't
    match an exact name and falls through to the FTS branch, which preserves the
    documented FTS-query capability.
    """
    rows: dict[int, sqlite3.Row] = {}
    if "::" in test_id:
        left, right = (part.strip() for part in test_id.rsplit("::", 1))
        for row in conn.execute(
            _EXACT_SELECT + "tc.name = ? AND (tc.classname = ? OR tc.file_path = ?)",
            (right, left, left),
        ):
            rows[row["cid"]] = row
        if not rows:
            _collect_fts(conn, right, rows)
    else:
        for row in conn.execute(_EXACT_SELECT + "tc.name = ?", (test_id,)):
            rows[row["cid"]] = row
        if not rows:
            _collect_fts(conn, test_id, rows)
    return rows


def test_failure_lookup(conn, test_id: str, limit: int = domain.DEFAULT_LIMIT,
                        *, config: dict | None = None) -> dict:
    """Failure history of one test, by identifier or FTS query.

    The identifier-resolution order (exact ``classname::name`` / ``file::name`` /
    bare name, each with an FTS fall-back) lives in ``_resolve_rows``. ``config``
    is the resolved tool config injected by the caller (the handler in
    ``__init__``); ``None`` means "use the documented defaults" — what direct
    callers and tests get without having to touch any module-level state.
    """
    cfg = config or {}
    test_id = _validate_str(test_id, "test_id")
    limit = _coerce_int(limit, default=domain.DEFAULT_LIMIT, lo=domain.MIN_LIMIT, hi=domain.MAX_LIMIT)
    max_trace = _coerce_int(cfg.get("max_stack_trace_chars", domain.DEFAULT_MAX_STACK_TRACE_CHARS),
                            default=domain.DEFAULT_MAX_STACK_TRACE_CHARS, lo=0, hi=None)

    all_rows = list(_resolve_rows(conn, test_id).values())
    # An exact id resolves to a single test; a fuzzy FTS fallback can match rows
    # from several distinct tests, in which case the counts below aggregate
    # across them. Surface how many distinct (classname, name) tests matched so
    # that breadth is explicit rather than silently folded into one figure.
    matched_tests = len({(r["classname"], r["name"]) for r in all_rows})
    failed = sorted(
        (r for r in all_rows if r["status"] in domain.FAIL_STATUSES),
        key=_row_timestamp_key,
        reverse=True,
    )

    failures_out = [
        {
            "run_id": r["run_id"],
            "timestamp": r["run_timestamp"] or r["ingested_at"],
            "status": r["status"],
            "classname": r["classname"],
            "name": r["name"],
            "file_path": r["file_path"],
            "failure_type": r["failure_type"],
            "message": r["failure_message"],
            "stack_trace_excerpt": _truncate(r["stack_trace"], max_trace),
        }
        for r in failed[:limit]
    ]

    return {
        "test_id": test_id,
        "matched_tests": matched_tests,
        "total_runs": len(all_rows),
        "failure_count": len(failed),
        "last_failure_at": _row_timestamp(failed[0]) if failed else None,
        "failures": failures_out,
    }


# ---------------------------------------------------------------------------
# Tool: module_failure_history
# ---------------------------------------------------------------------------


# Core grouped query (FROM/WHERE/GROUP/HAVING). Wrapped below with a
# ``COUNT(*) OVER ()`` window — which SQLite evaluates over the whole grouped
# result *before* applying LIMIT — so a single execution yields both the total
# number of qualifying tests (``total_groups``) and the bounded, ordered
# top-offenders slice. Counting and fetching as two statements would instead run
# this aggregation twice. ``domain.FAIL_STATUSES_SQL`` is the shared
# failure-status definition rendered as a static SQL fragment, and ``_ET_TR`` is
# the ``tr.``-qualified "effective timestamp" expression (run_timestamp, else
# ingested_at) from ``domain.effective_time`` — shared with the CLI so the rule
# cannot drift between where it is queried and where it is pruned.
_ET_TR = domain.effective_time("tr.")
_MODULE_CORE = (
    "SELECT tc.name AS name, tc.file_path AS file_path, tc.classname AS classname, "
    "       COUNT(*) AS total_runs, "
    "       SUM(CASE WHEN tc.status IN " + domain.FAIL_STATUSES_SQL + " THEN 1 ELSE 0 END) AS failure_count, "
    "       MAX(CASE WHEN tc.status IN " + domain.FAIL_STATUSES_SQL + " "
    "                THEN " + _ET_TR + " END) AS last_failure_at "
    "FROM test_cases tc JOIN test_runs tr ON tr.id = tc.run_id "
    "WHERE tc.file_path LIKE ? ESCAPE '\\' "
    "  AND datetime(" + _ET_TR + ") >= datetime(?) "
    "GROUP BY tc.file_path, tc.name, tc.classname "
    "HAVING failure_count >= ?"
)
_MODULE_SQL = (
    "SELECT m.*, COUNT(*) OVER () AS total_groups FROM (" + _MODULE_CORE + ") m "
    "ORDER BY failure_count DESC, total_runs DESC, name ASC LIMIT ?"
)


def _resolve_window_start(since, config: dict) -> str:
    """Resolve the window's start timestamp (naive-UTC ISO seconds).

    A ``None`` or blank ``since`` means "use the default window",
    ``default_lookback_days`` ago (read from the injected ``config``, falling back
    to ``domain.DEFAULT_LOOKBACK_DAYS``); any other value is validated as ISO-8601
    (raising on garbage rather than silently widening the window).
    """
    if since is None or (isinstance(since, str) and not since.strip()):
        lookback = _coerce_int(config.get("default_lookback_days", domain.DEFAULT_LOOKBACK_DAYS),
                               default=domain.DEFAULT_LOOKBACK_DAYS, lo=0, hi=None)
        window_dt = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=lookback)
        return window_dt.isoformat(timespec="seconds")
    return parse_iso_window(since, "since")


def module_failure_history(conn, path: str, since: str | None = None,
                           min_failures: int = domain.DEFAULT_MIN_FAILURES,
                           *, config: dict | None = None) -> dict:
    """Aggregate failures across tests whose ``file_path`` starts with ``path``.

    The time window starts at ``since`` (ISO-8601), defaulting to
    ``default_lookback_days`` ago. Timestamps are compared via SQLite's
    ``datetime()`` so ``T``- and space-separated forms compare consistently.
    ``config`` is the resolved tool config injected by the caller; ``None`` means
    "use the documented defaults".
    """
    cfg = config or {}
    path = _validate_path(path)
    min_failures = _coerce_int(min_failures, default=domain.DEFAULT_MIN_FAILURES, lo=1, hi=None)
    window_start = _resolve_window_start(since, cfg)

    params = (_like_prefix(path), window_start, min_failures)
    rows = conn.execute(_MODULE_SQL, params + (_MAX_OFFENDERS,)).fetchall()
    # COUNT(*) OVER () is identical on every row — the total number of qualifying
    # groups, computed before LIMIT; with no rows there are no failing groups.
    total = rows[0]["total_groups"] if rows else 0

    offenders = [
        {
            "name": r["name"],
            "file_path": r["file_path"],
            "classname": r["classname"],
            "failure_count": r["failure_count"],
            "total_runs": r["total_runs"],
            "last_failure_at": r["last_failure_at"],
        }
        for r in rows
    ]

    return {
        "path": path,
        "window_start": window_start,
        "tests_with_failures": total,
        "top_offenders": offenders,
        "truncated": total > _MAX_OFFENDERS,
    }
