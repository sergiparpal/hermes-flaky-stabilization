"""Timestamp normalization shared by every stage (stdlib only).

Every timestamp this plugin stores or compares is a *naive-UTC ISO-8601
seconds* string. The test-history database holds two shapes: ``run_timestamp``
is a ``T``-separated ISO-8601 string (from the XML) while ``ingested_at`` is
SQLite's space-separated ``CURRENT_TIMESTAMP``. A naive lexical comparison
mis-orders the two (``' '`` < ``'T'``), so every timestamp is first normalized
to the canonical ``T``-separated form; with that form a plain string sort is
also a chronological sort, which the pure detection core relies on.

This module is the SINGLE owner of that rule. It used to be copied into
``history/timeutil.py`` and ``detective/timeutil.py``; the copies had already
drifted (history's parser rejected the space-separated form detective
accepted), so both now re-export from here. Stdlib only — importable by the
pure ``detect`` core without pulling in any I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def iso_to_naive_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 string to a naive-UTC ``datetime``, or ``None``.

    Accepts both ``T``- and space-separated forms, a trailing ``Z``, and
    explicit offsets; a tz-aware result is converted to UTC and stripped of
    tzinfo. Returns ``None`` for a non-string, an empty string, or anything
    ``datetime.fromisoformat`` rejects.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # SQLite's CURRENT_TIMESTAMP is space-separated; normalize to the ISO 'T'
    # form fromisoformat understands (only the date/time separator, hence count=1).
    raw = raw.replace(" ", "T", 1)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def normalize_ts(value: str) -> str | None:
    """Return ``value`` as canonical naive-UTC ISO seconds, or ``None``.

    Used by the reader to canonicalize each row's effective timestamp before
    handing it to the pure detection core, so the core can compare timestamps
    as plain strings.
    """
    dt = iso_to_naive_utc(value)
    return dt.isoformat(timespec="seconds") if dt is not None else None


def window_cutoff(now: datetime, window_days: int) -> str:
    """The window's lower bound as canonical naive-UTC ISO seconds.

    ``now`` is a ``datetime`` (tz-aware or naive); the result is
    ``now - window_days`` rendered in the same canonical form as stored
    timestamps so the windowing comparison is apples-to-apples.
    """
    if now.tzinfo is not None:
        now = now.astimezone(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=int(window_days))
    return cutoff.isoformat(timespec="seconds")


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
