"""Profile-aware path resolution for the unified plugin.

Two storage roots exist by design (plan D4):

* ``<hermes_home>/test-history/`` — the keystone public data contract owned by
  the ported ``history`` stage (its own storage module resolves it; not here).
* ``<hermes_home>/flaky-stabilization/`` — everything private to this plugin:
  ``state.db``, ``config.json``, healer patches, the learned-patterns mirror.

Nothing is cached at module level: tests repoint ``HERMES_HOME`` per test.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_NAME = "flaky-stabilization"


def get_hermes_home() -> Path:
    """Return the profile-aware Hermes home.

    Prefer the official helpers when Hermes is importable; otherwise fall back
    to the ``HERMES_HOME`` env var, then ``~/.hermes``. Never hardcoded.

    The env value gets ``expandvars`` + ``expanduser`` applied: sources like a
    systemd ``EnvironmentFile`` or crontab set ``HERMES_HOME=~/hermes-data``
    with no shell expansion, and a literal ``~/...`` would otherwise become a
    *relative* path that ``get_data_dir`` mkdirs as ``./~`` under the CWD.
    """
    try:
        from hermes_constants import get_hermes_home as _core_home  # type: ignore

        return Path(_core_home())
    except Exception:
        pass
    try:
        from hermes_cli.utils import display_hermes_home  # type: ignore

        return Path(display_hermes_home())
    except Exception:
        pass
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(os.path.expanduser(os.path.expandvars(env_home)))
    return Path.home() / ".hermes"


def get_data_dir() -> Path:
    """``<hermes_home>/flaky-stabilization/`` — created owner-only.

    The directory holds captured test output, triage excerpts and incident
    rows that can contain sensitive data; keep it ``0700`` so other local
    users cannot read it (or the SQLite WAL/-shm sidecars). Best-effort: a
    filesystem that cannot represent POSIX modes must not break the plugin.
    """
    data_dir = get_hermes_home() / DATA_DIR_NAME
    # mode=0o700 on creation closes the window between mkdir (default 0755) and
    # the chmod below during which the dir — and any sidecar written into it — is
    # world-listable. 0o700 is umask-safe (no group/other bits to mask off). The
    # chmod stays as belt-and-suspenders for a dir that already exists 0755.
    data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    return data_dir


def _is_real_file_path(path: Path | str) -> bool:
    """False for the sentinel/URI paths SQLite accepts (``:memory:``, ``file:``)
    that have no on-disk file to permission-harden."""
    s = str(path)
    return bool(s) and s != ":memory:" and not s.startswith("file:")


def precreate_private(path: Path | str) -> None:
    """Create *path* ``0600`` *before* SQLite opens it, so a DB holding PII is
    never briefly exposed at the process umask (e.g. 0644). No-op if the file
    already exists or cannot be created; :func:`harden_db_files` chmods after
    connect regardless. Best-effort on non-POSIX mounts."""
    if not _is_real_file_path(path) or os.path.exists(path):
        return
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    except OSError:
        pass


def harden_db_files(path: Path | str) -> None:
    """Best-effort ``0600`` on a SQLite DB *and* its WAL/-shm/-journal sidecars.

    The sidecars are created by the first write transaction in WAL mode and hold
    the same recently-written rows as the main DB, so restricting only the main
    file leaves incident PII / captured test output group- or world-readable in
    the sidecars. Call after the schema is applied so the sidecars already exist.
    """
    if not _is_real_file_path(path):
        return
    base = str(path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = base + suffix
        try:
            if os.path.exists(p):
                os.chmod(p, 0o600)
        except OSError:
            pass


def restrict_file(path: Path | str) -> None:
    """Best-effort ``0600`` on a data file (DB, config).

    For a live SQLite DB prefer :func:`harden_db_files`, which also covers the
    WAL/-shm/-journal sidecars.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
