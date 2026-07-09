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
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def get_data_dir() -> Path:
    """``<hermes_home>/flaky-stabilization/`` — created owner-only.

    The directory holds captured test output, triage excerpts and incident
    rows that can contain sensitive data; keep it ``0700`` so other local
    users cannot read it (or the SQLite WAL/-shm sidecars). Best-effort: a
    filesystem that cannot represent POSIX modes must not break the plugin.
    """
    data_dir = get_hermes_home() / DATA_DIR_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    return data_dir


def restrict_file(path: Path | str) -> None:
    """Best-effort ``0600`` on a data file (DB, config)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
