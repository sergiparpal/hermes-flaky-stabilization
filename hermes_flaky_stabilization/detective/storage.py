"""Verdicts storage — a thin adapter over the unified ``state.db`` (plan D4).

The pure SQL helpers are module-level functions that each take an explicit
``conn`` (dependency-injected, trivially testable). Connection *ownership* lives
in the :class:`Storage` object: it opens one connection lazily and the caller that
constructs it owns its lifecycle — registration holds a long-lived instance for
the ``is_flaky`` tool path, while each short-lived CLI command uses one as a
context manager. There is deliberately no module-global connection (and no
test-only reset hook): global state was the one piece coupling every caller to a
hidden singleton.

Unified-plugin adaptation: the legacy ``<hermes_home>/flaky-detective/verdicts.db``
is replaced by the consolidated ``state.db``, which carries the identical
``flaky_verdicts`` / ``scan_runs`` DDL as migration step 1. The SQL helpers and
the ``Storage`` lifecycle below are verbatim from the legacy plugin.
"""

import sqlite3
from dataclasses import fields as _dc_fields
from pathlib import Path

# The verdict field set has a single owner: the ``Verdict`` dataclass. Imported at
# module load so ``_VERDICT_COLS`` can be derived from it below. No import cycle —
# ``detect`` imports only the import-free ``domain``, never ``storage``.
from .detect import Verdict

# ---------------------------------------------------------------------------
# Paths (profile-aware, unified data dir)
# ---------------------------------------------------------------------------


def get_hermes_home() -> Path:
    """Return the profile-aware Hermes home (delegates to the unified resolver)."""
    from .. import paths

    return paths.get_hermes_home()


def get_storage_dir() -> Path:
    """The unified plugin's owner-only data directory."""
    from .. import paths

    return paths.get_data_dir()


def get_db_path() -> Path:
    """Path to the consolidated state database holding the verdict tables."""
    from ..storage import state

    return state.state_db_path()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def open_connection(db_path=None) -> sqlite3.Connection:
    """Open ``state.db`` (creating it and applying the schema); return the conn.

    No global state — the caller owns the returned connection's lifecycle (close
    it, or let a :class:`Storage` own it). ``db_path`` defaults to
    :func:`get_db_path`. The unified connection factory keeps the legacy
    properties: ``check_same_thread=False`` (the ``is_flaky`` tool may run in the
    gateway's async worker pool), WAL journal mode (tool lookups proceed
    concurrently with a ``scan`` writer in another process), owner-only file
    permissions, schema applied idempotently on open.
    """
    from ..storage import state

    return state.connect(db_path)


class Storage:
    """Owns one lazily-opened verdicts-DB connection (replaces the old singleton).

    Constructing a ``Storage`` does no I/O — the connection opens on first use —
    so registration can cheaply hold a long-lived instance for the ``is_flaky``
    tool path while each short-lived CLI command uses one as a context manager
    (``with Storage() as store: ...``). Lifecycle is explicit: whoever constructs
    it owns it, so there is no hidden module global to reset between tests.

    The verdict methods delegate to the module-level SQL helpers, passing the
    owned connection — those helpers stay independently unit-testable with any
    ``conn``.
    """

    def __init__(self, db_path=None):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        """The owned connection, opened on first access."""
        if self._conn is None:
            self._conn = open_connection(self._db_path)
        return self._conn

    def close(self) -> None:
        """Close the connection if it was opened; a no-op otherwise."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- verdict operations (delegate to the conn-taking helpers below) --------

    def resolve_verdicts(self, test_id: str) -> list:
        return resolve_verdicts(self.conn, test_id)

    def replace_verdicts(self, verdicts) -> None:
        return replace_verdicts(self.conn, verdicts)

    def record_scan_run(self, **kwargs) -> int:
        return record_scan_run(self.conn, **kwargs)

    def read_flaky_keys(self) -> set:
        return read_flaky_keys(self.conn)

    def get_verdict(self, test_key: str):
        return get_verdict(self.conn, test_key)

    def read_verdicts(self, statuses=None) -> list:
        return read_verdicts(self.conn, statuses)

    def last_scan_run(self):
        return last_scan_run(self.conn)

    def count_by_status(self) -> dict:
        return count_by_status(self.conn)


# ---------------------------------------------------------------------------
# Verdict snapshot writes / reads
# ---------------------------------------------------------------------------

# Column order shared by the snapshot INSERT and the Verdict->row mapping.
# Derived from the ``Verdict`` dataclass so the field set has a single owner: add
# or rename a verdict field in ``detect.Verdict`` and the INSERT follows
# automatically (a test pins this against the DDL). ``computed_at`` is a DB-only
# column (its DEFAULT CURRENT_TIMESTAMP applies) and is intentionally absent from
# the dataclass, so it never appears here.
_VERDICT_COLS = tuple(f.name for f in _dc_fields(Verdict))
_VERDICT_INSERT = (
    "INSERT INTO flaky_verdicts (" + ", ".join(_VERDICT_COLS) + ") "
    "VALUES (" + ", ".join("?" * len(_VERDICT_COLS)) + ")"
)


def replace_verdicts(conn: sqlite3.Connection, verdicts) -> None:
    """Atomically replace the whole ``flaky_verdicts`` snapshot.

    A scan reflects the *current* state of the world, so the table is overwritten
    wholesale (delete + insert) inside a single transaction: readers see either
    the old snapshot or the new one, never a half-written mix.
    """
    params = [tuple(getattr(v, c) for c in _VERDICT_COLS) for v in verdicts]
    with conn:  # transaction: commit on success, rollback on error
        conn.execute("DELETE FROM flaky_verdicts")
        if params:
            conn.executemany(_VERDICT_INSERT, params)


def record_scan_run(conn: sqlite3.Connection, *, window_days: int, min_fails: int,
                    include_errors: bool, source_schema_version, tests_examined: int,
                    flaky_found: int) -> int:
    """Append one ``scan_runs`` audit row; return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO scan_runs (window_days, min_fails, include_errors, "
            "source_schema_version, tests_examined, flaky_found) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(window_days), int(min_fails), 1 if include_errors else 0,
             source_schema_version, int(tests_examined), int(flaky_found)),
        )
    return int(cur.lastrowid)


def read_flaky_keys(conn: sqlite3.Connection) -> set:
    """The set of ``test_key`` currently classified ``flaky`` (for change diffs)."""
    from . import domain  # lazy: domain is import-free, this just avoids top churn

    rows = conn.execute(
        "SELECT test_key FROM flaky_verdicts WHERE status = ?",
        (domain.VERDICT_FLAKY,),
    ).fetchall()
    return {r["test_key"] for r in rows}


def get_verdict(conn: sqlite3.Connection, test_key: str):
    """The verdict row for ``test_key``, or ``None`` if there is none."""
    return conn.execute(
        "SELECT * FROM flaky_verdicts WHERE test_key = ?", (test_key,)
    ).fetchone()


def resolve_verdicts(conn: sqlite3.Connection, test_id: str) -> list:
    """Verdict rows matching ``test_id``, worst-first (most failures first).

    The resolution the ``is_flaky`` tool needs, owned here with the rest of the
    ``flaky_verdicts`` SQL rather than reimplemented in the tool layer. Resolution
    order: an exact ``test_key`` (``classname::name``); else, for a ``left::right``
    identifier, ``name = right`` with ``classname`` *or* ``file_path`` equal to
    ``left`` (so ``file_path::name`` works even though the key is built from
    classname); else a bare ``name`` match. Every value binds through ``?``.
    """
    rows = conn.execute(
        "SELECT * FROM flaky_verdicts WHERE test_key = ? ORDER BY fails DESC, runs DESC",
        (test_id,),
    ).fetchall()
    if rows:
        return rows
    if "::" in test_id:
        left, right = (p.strip() for p in test_id.rsplit("::", 1))
        return conn.execute(
            "SELECT * FROM flaky_verdicts WHERE name = ? AND (classname = ? OR file_path = ?) "
            "ORDER BY fails DESC, runs DESC, test_key ASC",
            (right, left, left),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM flaky_verdicts WHERE name = ? ORDER BY fails DESC, runs DESC, test_key ASC",
        (test_id,),
    ).fetchall()


def read_verdicts(conn: sqlite3.Connection, statuses=None) -> list:
    """All verdict rows, optionally filtered to ``statuses`` (an iterable).

    Ordered worst-first (most failures, then most runs, then key) for stable,
    human-friendly listings.
    """
    sql = "SELECT * FROM flaky_verdicts"
    params: tuple = ()
    if statuses:
        statuses = tuple(statuses)
        placeholders = ", ".join("?" * len(statuses))
        sql += f" WHERE status IN ({placeholders})"
        params = statuses
    sql += " ORDER BY fails DESC, runs DESC, test_key ASC"
    return conn.execute(sql, params).fetchall()


def last_scan_run(conn: sqlite3.Connection):
    """The most recent ``scan_runs`` row, or ``None`` if no scan has run."""
    return conn.execute(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def count_by_status(conn: sqlite3.Connection) -> dict:
    """``{status: count}`` over ``flaky_verdicts`` (empty dict if none)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM flaky_verdicts GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
