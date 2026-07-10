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
    """Return the profile-aware Hermes home (delegates to the unified resolver).

    This used to carry its own lookup chain, which omitted ``hermes_constants``
    and — the bug — the ``expanduser``/``expandvars`` of ``$HERMES_HOME``, so
    ``HERMES_HOME=~/hermes-data`` put ``history.db`` under a literal ``./~``
    directory while ``state.db`` (via ``paths``) went to the real home.
    """
    from .. import paths

    return paths.get_hermes_home()


def get_storage_dir() -> Path:
    storage_dir = get_hermes_home() / "test-history"
    # mode=0o700 on creation closes the mkdir(0755)→chmod window; 0o700 is
    # umask-safe. The chmod below stays for an already-existing 0755 dir.
    storage_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
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


def _unified_overrides() -> dict[str, Any]:
    """``history``-section keys explicitly set in the unified plugin config.

    The unified ``<hermes_home>/flaky-stabilization/config.json`` (plan D5)
    carries a ``history`` section too. Only keys the user actually wrote there
    may override the layers below — ``load_config`` fills every section with
    defaults, so key presence is taken from the raw file while the values come
    from ``load_config`` (which type-coerces them). Any failure degrades to
    "no overrides" so a broken unified config can never take down the keystone
    tools.
    """
    try:
        from .. import config as unified_config

        path = unified_config.config_path()
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        section = raw.get("history") if isinstance(raw, dict) else None
        if not isinstance(section, dict):
            return {}
        resolved = unified_config.load_config().get("history", {})
        return {k: resolved[k] for k in section if k in resolved}
    except Exception:
        return {}


def get_config() -> dict[str, Any]:
    """Resolved config, cached after first read.

    Precedence (highest wins): the ``history`` section of the unified
    ``flaky-stabilization/config.json`` (only keys explicitly set there), then
    the legacy ``test-history/config.json``, then ``DEFAULT_CONFIG``. A
    malformed file at either location degrades to the layers below rather than
    raising, and nothing changes for existing installs until a unified
    ``history`` section is written.
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
    _config = {**DEFAULT_CONFIG, **user_cfg, **_unified_overrides()}
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
        from .. import paths
        from .schema import apply_schema  # lazy import (hard-constraint #2)

        db_path = get_db_path()
        # Own the file 0600 *before* SQLite opens it, closing the create-at-umask
        # → chmod window during which captured test output would be exposed.
        paths.precreate_private(db_path)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA recursive_triggers = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA case_sensitive_like = ON")
        apply_schema(conn)
        # Owner-only: the DB can hold sensitive captured test output. Harden the
        # DB *and* its WAL/-shm/-journal sidecars — created by the first write in
        # WAL mode, so this runs after apply_schema.
        paths.harden_db_files(db_path)
        _connection = conn
    return _connection


def reset_for_tests() -> None:
    """Test-only hook: close and clear the module-level singletons."""
    global _connection, _config
    if _connection is not None:
        _connection.close()
    _connection = None
    _config = None
