"""Timestamp normalization shared by the ingest and query/CLI layers.

Every timestamp this plugin stores or compares is a *naive-UTC ISO-8601 seconds*
string, so that pytest's ``T``-separated values and SQLite's space-separated
``ingested_at`` compare consistently through ``datetime()``. Both the lenient
ingest path (unparseable → fall back to file mtime) and the strict query/CLI
path (unparseable → structured error) normalize the same way; this module is the
single source of that rule, so the two paths cannot drift apart.
"""

from datetime import UTC, datetime


def iso_to_naive_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 string to a naive-UTC ``datetime``, or ``None``.

    A trailing ``Z`` and explicit offsets are accepted; a tz-aware result is
    converted to UTC and stripped of its tzinfo. Returns ``None`` for anything
    ``datetime.fromisoformat`` rejects.
    """
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def parse_iso_window(value, field: str = "since") -> str:
    """Validate an ISO-8601 ``value`` and return naive-UTC ISO seconds.

    Raises ``ValueError`` (naming ``field``) on a non-string, empty, or
    unparseable value, so callers can surface a structured error rather than
    silently widening a time window. Used by ``module_failure_history`` (the
    ``since`` arg) and the CLI ``prune`` command (the ``--before`` arg).
    """
    if not isinstance(value, str):
        raise ValueError(f"`{field}` must be an ISO-8601 date/time string")
    raw = value.strip()
    if not raw:
        raise ValueError(f"`{field}` must not be empty")
    dt = iso_to_naive_utc(raw)
    if dt is None:
        raise ValueError(f"`{field}` is not a valid ISO-8601 date/time: {value!r}")
    return dt.isoformat(timespec="seconds")
