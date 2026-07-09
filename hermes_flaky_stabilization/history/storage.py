"""SQLite connection and configuration management.

No class hierarchy — just module-level functions and two lazily-initialized
singletons (the connection and the resolved config). Storage paths are derived
from Hermes's profile-aware home, never hardcoded (hard-constraint #5).
"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import domain

# Module-level singletons. Opened on first use; closed at process exit by
# Python's normal cleanup (and explicitly in tests via reset_for_tests).
_connection: sqlite3.Connection | None = None
_config: dict[str, Any] | None = None

# The two tunables mirror their single source of truth in ``domain`` (the query
# layer reads the same defaults); ``db_path_override`` is storage-only so it has
# no domain constant.
DEFAULT_CONFIG: dict[str, Any] = {
    "default_lookback_days": domain.DEFAULT_LOOKBACK_DAYS,
    "max_stack_trace_chars": domain.DEFAULT_MAX_STACK_TRACE_CHARS,
    "db_path_override": None,
}


# ---------------------------------------------------------------------------
# Paths (profile-aware)
# ---------------------------------------------------------------------------


def get_hermes_home() -> Path:
    """Return the profile-aware Hermes home.

    Prefer the official helper if Hermes is importable; otherwise fall back to
    the ``HERMES_HOME`` env var, then ``~/.hermes``. Never hardcoded.
    """
    try:
        from hermes_cli.utils import display_hermes_home  # type: ignore

        return Path(display_hermes_home())
    except Exception:
        # Hermes not importable (e.g. standalone dev/test) or helper moved.
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def get_storage_dir() -> Path:
    storage_dir = get_hermes_home() / "test-history"
    storage_dir.mkdir(parents=True, exist_ok=True)
    # The DB holds captured test output (stack traces / failure messages) that
    # can contain sensitive data; keep the directory owner-only so other local
    # users cannot read it (or the WAL/-shm sidecars). Best-effort: a filesystem
    # that cannot represent POSIX modes (e.g. a Windows mount) must not break
    # ingestion.
    try:
        os.chmod(storage_dir, 0o700)
    except OSError:
        pass
    return storage_dir


def get_db_path() -> Path:
    cfg = get_config()
    override = cfg.get("db_path_override")
    if override:
        # Confine the override to within the Hermes home: a (possibly attacker-
        # writable) config file must not be able to redirect the DB — and the
        # mkdir below — to an arbitrary filesystem location. realpath resolves
        # symlinks and '..' before the containment check.
        home = Path(os.path.realpath(str(get_hermes_home())))
        path = Path(os.path.realpath(str(override)))
        if path != home and home not in path.parents:
            raise ValueError(
                f"db_path_override must be within the Hermes home ({home}); got {override!r}"
            )
        # Ensure the override's parent exists, mirroring get_storage_dir() for
        # the default path; otherwise sqlite3.connect raises "unable to open
        # database file" for a not-yet-existing directory.
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return get_storage_dir() / "history.db"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def get_config() -> dict[str, Any]:
    """Resolved config: user ``config.json`` merged over ``DEFAULT_CONFIG``.

    Cached after first read. A malformed config file degrades to defaults
    rather than raising.
    """
    global _config
    if _config is not None:
        return _config
    config_path = get_storage_dir() / "config.json"
    user_cfg: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                user_cfg = loaded
        except Exception:
            user_cfg = {}
    _config = {**DEFAULT_CONFIG, **user_cfg}
    return _config


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_connection() -> sqlite3.Connection:
    """Lazy singleton connection with the schema applied.

    ``check_same_thread=False`` because tools may run in the gateway's async
    worker pool. ``foreign_keys`` + ``recursive_triggers`` are both ON so that
    FK-cascade deletes (e.g. pruning a run) also fire the FTS5 sync triggers,
    keeping ``test_cases_fts`` consistent. WAL journal mode lets the gateway's
    read-only tool queries proceed concurrently with a CLI ingest/prune writer
    in another process instead of blocking on the rollback-journal lock.
    ``case_sensitive_like`` is ON so ``module_failure_history``'s
    ``file_path LIKE 'prefix%'`` can use the ``idx_cases_module`` index as a
    range seek; SQLite disables that optimization for the default
    case-insensitive LIKE, forcing a full scan. The only LIKE in the codebase is
    that path-prefix match and file paths are case-sensitive, so this is both
    safe and more correct.
    """
    global _connection
    if _connection is None:
        from .schema import apply_schema  # lazy import (hard-constraint #2)

        db_path = get_db_path()
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # Owner-only: the DB can hold sensitive captured test output. The 0o700
        # directory is the primary guard (it also covers the -wal/-shm sidecars);
        # this narrows the DB file itself too. Best-effort on non-POSIX mounts.
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA case_sensitive_like = ON")
        apply_schema(conn)
        _connection = conn
    return _connection


def reset_for_tests() -> None:
    """Test-only hook: close and clear the module-level singletons."""
    global _connection, _config
    if _connection is not None:
        _connection.close()
    _connection = None
    _config = None
