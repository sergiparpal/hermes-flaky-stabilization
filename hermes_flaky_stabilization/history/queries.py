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

# ``_ET_TR`` is the ``tr.``-qualified "effective timestamp" expression
# (run_timestamp, else ingested_at) from ``domain.effective_time`` — shared with
# the CLI so the rule cannot drift between where it is queried and where it is
# pruned.
_ET_TR = domain.effective_time("tr.")

# Shared column list, concatenated (not f-string interpolated) into the
# failure-fetching statement below.
_CASE_COLS = (
    "tc.id AS cid, tc.run_id, tc.classname, tc.name, tc.file_path, tc.status, "
    "tc.failure_message, tc.failure_type, tc.stack_trace, "
    "tr.run_timestamp, tr.ingested_at"
)
# FROM/WHERE fragments shared by the aggregate and failure-fetching statements
# of ``test_failure_lookup`` (the exact form gets its predicate appended; the
# FTS form may get further ``AND`` clauses appended after the MATCH).
_EXACT_FROM_WHERE = (
    "FROM test_cases tc "
    "JOIN test_runs tr ON tr.id = tc.run_id WHERE "
)
_FTS_FROM_WHERE = (
    "FROM test_cases_fts f "
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


def _resolve_predicate(test_id: str) -> tuple[str, tuple, str]:
    """Resolve ``test_id`` to ``(from_where, params, fts_query)``.

    ``from_where`` + ``params`` is the exact-match candidate set:
      * ``classname::name`` or ``file_path::name`` → exact structured match;
      * otherwise → exact name match.
    ``fts_query`` is the FTS fallback query (the name part) used when the exact
    set turns out empty.

    Exact matching is always attempted first and is safe for any input — the
    value only ever flows through a ``?`` placeholder — so a test whose literal
    name contains an FTS metacharacter (``*``, ``"``, ``^``, ``AND`` …) is still
    found by id. An explicit FTS query such as ``login AND oauth`` simply doesn't
    match an exact name and falls through to the FTS branch, which preserves the
    documented FTS-query capability.
    """
    if "::" in test_id:
        left, right = (part.strip() for part in test_id.rsplit("::", 1))
        return (
            _EXACT_FROM_WHERE + "tc.name = ? AND (tc.classname = ? OR tc.file_path = ?)",
            (right, left, left),
            right,
        )
    return _EXACT_FROM_WHERE + "tc.name = ?", (test_id,), test_id


def _lookup_counts(conn, from_where: str, params: tuple) -> tuple[int, int, int]:
    """``(total_runs, failure_count, matched_tests)`` for one candidate set.

    Two small aggregate statements instead of fetching every candidate row: the
    full (untruncated) stack traces never leave SQLite just to be counted.
    ``matched_tests`` counts distinct ``(classname, name)`` pairs — a subquery,
    because ``COUNT(DISTINCT ...)`` takes a single expression and concatenation
    would collapse NULL classnames.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total_runs, "
        "SUM(CASE WHEN tc.status IN " + domain.FAIL_STATUSES_SQL + " THEN 1 ELSE 0 END) "
        "AS failure_count " + from_where,
        params,
    ).fetchone()
    matched = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT tc.classname, tc.name " + from_where + ")",
        params,
    ).fetchone()[0]
    return row["total_runs"], row["failure_count"] or 0, matched


def _lookup_failures(conn, from_where: str, params: tuple, limit: int) -> list:
    """The ``limit`` most recent failing rows of one candidate set.

    Ordering and limiting happen in SQL so at most ``limit`` stack traces are
    ever fetched. ``datetime()`` normalizes the ``T``-separated ``run_timestamp``
    and space-separated ``ingested_at`` forms so they compare chronologically
    (the same normalization ``module_failure_history`` applies to its window),
    and it yields NULL for an unparseable timestamp — which ``DESC`` sorts last,
    matching the previous Python sort's ``datetime.min`` fallback. ``tc.id``
    breaks ties deterministically (insertion order, which the previous stable
    sort preserved).
    """
    return conn.execute(
        "SELECT " + _CASE_COLS + " " + from_where
        + " AND tc.status IN " + domain.FAIL_STATUSES_SQL
        + " ORDER BY datetime(" + _ET_TR + ") DESC, tc.id ASC LIMIT ?",
        params + (limit,),
    ).fetchall()


def test_failure_lookup(conn, test_id: str, limit: int = domain.DEFAULT_LIMIT,
                        *, config: dict | None = None) -> dict:
    """Failure history of one test, by identifier or FTS query.

    The identifier-resolution order (exact ``classname::name`` / ``file::name`` /
    bare name, each with an FTS fall-back) lives in ``_resolve_predicate``.
    ``config`` is the resolved tool config injected by the caller (the handler in
    ``__init__``); ``None`` means "use the documented defaults" — what direct
    callers and tests get without having to touch any module-level state.
    """
    cfg = config or {}
    test_id = _validate_str(test_id, "test_id")
    limit = _coerce_int(limit, default=domain.DEFAULT_LIMIT, lo=domain.MIN_LIMIT, hi=domain.MAX_LIMIT)
    max_trace = _coerce_int(cfg.get("max_stack_trace_chars", domain.DEFAULT_MAX_STACK_TRACE_CHARS),
                            default=domain.DEFAULT_MAX_STACK_TRACE_CHARS, lo=0, hi=None)

    from_where, params, fts_query = _resolve_predicate(test_id)
    total_runs, failure_count, matched_tests = _lookup_counts(conn, from_where, params)
    if total_runs == 0:
        # Nothing matched exactly — fall back to FTS on the name part. An exact
        # id resolves to a single test; the fuzzy fallback can match rows from
        # several distinct tests, in which case the counts aggregate across
        # them (matched_tests surfaces that breadth explicitly). Best-effort:
        # malformed FTS syntax (e.g. an injection attempt) yields empty
        # results, not an exception.
        from_where, params = _FTS_FROM_WHERE, (fts_query,)
        try:
            total_runs, failure_count, matched_tests = _lookup_counts(conn, from_where, params)
        except sqlite3.OperationalError:
            # e.g. "fts5: syntax error" from an injection-y / malformed query.
            total_runs = failure_count = matched_tests = 0
    # limit >= MIN_LIMIT (1), so whenever failure_count > 0 this fetches at
    # least the most recent failing row (which also supplies last_failure_at).
    failed = _lookup_failures(conn, from_where, params, limit) if failure_count else []

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
        for r in failed
    ]

    return {
        "test_id": test_id,
        "matched_tests": matched_tests,
        "total_runs": total_runs,
        "failure_count": failure_count,
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
# failure-status definition rendered as a static SQL fragment, and ``_ET_TR``
# (defined with the shared fragments above) is the ``tr.``-qualified "effective
# timestamp" expression. ``last_failure_at`` wraps it in ``datetime()`` — the
# same normalization the WHERE clause applies — because a raw-string MAX() over
# mixed ``T``-separated ``run_timestamp`` and space-separated ``ingested_at``
# values mis-orders same-date ties ('T' > ' '); this also makes the output
# format uniform.
_MODULE_CORE = (
    "SELECT tc.name AS name, tc.file_path AS file_path, tc.classname AS classname, "
    "       COUNT(*) AS total_runs, "
    "       SUM(CASE WHEN tc.status IN " + domain.FAIL_STATUSES_SQL + " THEN 1 ELSE 0 END) AS failure_count, "
    "       MAX(CASE WHEN tc.status IN " + domain.FAIL_STATUSES_SQL + " "
    "                THEN datetime(" + _ET_TR + ") END) AS last_failure_at "
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
