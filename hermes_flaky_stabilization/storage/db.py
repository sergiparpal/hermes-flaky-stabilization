"""The one private-WAL SQLite connection recipe.

Every database this plugin opens — ``state.db`` and the public ``history.db`` —
wants the same owner-only, WAL, ``Row``-factory connection. That recipe was
hand-rolled in four places (``state.connect``, ``history.storage``,
``incidents.store``, ``triage.patterns``); this is the single owner.

It deliberately does NOT apply a schema or harden the sidecars: the schema is
per-database, and hardening must run *after* the schema's first write creates
the WAL/-shm files. Callers do ``conn = open_private(path, ...)`` → apply their
schema → ``paths.harden_db_files(path)``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .. import paths


def open_private(
    db_path: Path | str,
    *,
    uri: bool = False,
    check_same_thread: bool = False,
    timeout: float = 15,
    extra_pragmas: Iterable[str] = (),
) -> sqlite3.Connection:
    """Open an owner-only, WAL, ``Row``-factory SQLite connection (no schema).

    Creates the file ``0600`` *before* SQLite opens it (closing the
    create-at-umask window during which sensitive data could be exposed), sets
    ``journal_mode=WAL`` and any *extra_pragmas* (each best-effort). The caller
    owns applying the schema and then calling ``paths.harden_db_files`` so the
    WAL/-shm sidecars are locked too.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Own the file 0600 before SQLite opens it (no-op for :memory:/file: URIs).
    paths.precreate_private(path)
    conn = sqlite3.connect(
        str(path), timeout=timeout, check_same_thread=check_same_thread, uri=uri
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # A filesystem that refuses WAL: fall back to the default journal mode.
        pass
    for pragma in extra_pragmas:
        try:
            conn.execute(pragma)
        except sqlite3.OperationalError:
            pass
    return conn
