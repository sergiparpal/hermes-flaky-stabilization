"""Configuration resolution and test-history DB location.

Unified-plugin adaptation: the legacy ``<hermes_home>/flaky-detective/config.json``
is replaced by the ``detective`` section of the unified
``<hermes_home>/flaky-stabilization/config.json`` (plan Appendix B/C). The keys,
defaults and coercion rules are verbatim from the legacy plugin; only where the
raw dict is read from / written to changed. Resolution is stateless (the file is
read on each call); a malformed config file degrades to defaults rather than
raising.
"""

import json
import os
from pathlib import Path

from . import domain

_SECTION = "detective"

# Default filename of the test-history database, mirroring the sibling plugin's
# ``storage.get_db_path`` (``<hermes_home>/test-history/history.db``). Re-derived
# here, never imported, to keep the GPL/data-only boundary intact.
TEST_HISTORY_DB_FILENAME = "history.db"

# Resolved config = these defaults, overlaid by the user's config.json. The
# detection tunables mirror their single source of truth in ``domain``.
DEFAULT_CONFIG: dict = {
    "window_days": domain.DEFAULT_WINDOW_DAYS,
    "min_fails": domain.DEFAULT_MIN_FAILS,
    "include_errors": domain.DEFAULT_INCLUDE_ERRORS,
    "deliver": domain.DEFAULT_DELIVER,
    "schedule": domain.DEFAULT_SCHEDULE,
    "report_scope": domain.DEFAULT_REPORT_SCOPE,
    # null → auto-resolve to <hermes_home>/test-history/history.db.
    "test_history_db_path": None,
    "source_schema_version": domain.EXPECTED_SOURCE_SCHEMA_VERSION,
}


def config_path() -> Path:
    """Path to the unified user config file (may not exist)."""
    from .. import config as unified_config

    return unified_config.config_path()


# ---------------------------------------------------------------------------
# Value coercion — keep a hand-edited config from crashing a later int()/bool()
# ---------------------------------------------------------------------------

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
    return default


def _as_str(value, default: str) -> str:
    return value if isinstance(value, str) else default


def _coerce_config(cfg: dict) -> dict:
    """Coerce known keys to their expected types, degrading to the default on a
    malformed value, so a hand-edited ``config.json`` can never crash a later
    ``int()``/``bool()`` in the scan path. Note ``"false"`` (a JSON *string*) is a
    typo, not a truthy value: it coerces to ``False``, never ``bool("false")``.
    """
    out = dict(cfg)
    for key in ("window_days", "min_fails", "source_schema_version"):
        out[key] = _as_int(out.get(key), DEFAULT_CONFIG[key])
    out["include_errors"] = _as_bool(out.get("include_errors"), DEFAULT_CONFIG["include_errors"])
    for key in ("deliver", "schedule", "report_scope"):
        out[key] = _as_str(out.get(key), DEFAULT_CONFIG[key])
    raw_path = out.get("test_history_db_path")
    out["test_history_db_path"] = raw_path if (raw_path is None or isinstance(raw_path, str)) else None
    return out


def _read_raw_config() -> dict:
    """The user's ``detective`` config section as a dict, or ``{}`` if
    absent/unreadable/malformed."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    section = loaded.get(_SECTION)
    return section if isinstance(section, dict) else {}


def get_config() -> dict:
    """User ``config.json`` merged over :data:`DEFAULT_CONFIG` (defaults on error)."""
    return _coerce_config({**DEFAULT_CONFIG, **_read_raw_config()})


def write_config(updates: dict) -> dict:
    """Merge ``updates`` into the on-disk ``detective`` section and return the
    resolved result.

    Persists only the *user* layer (existing section contents merged with
    ``updates``); defaults are always re-applied at read time. Used by
    ``install-cron`` to record the resolved cron options so the shim's ``scan``
    uses them. Other sections of the unified file are preserved untouched; the
    write is atomic and owner-only (unified ``write_config``).
    """
    from .. import config as unified_config

    path = config_path()
    whole: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                whole = loaded
        except Exception:
            whole = {}
    section = whole.get(_SECTION)
    merged = {**(section if isinstance(section, dict) else {}), **updates}
    whole[_SECTION] = merged
    unified_config.write_config(whole)
    return _coerce_config({**DEFAULT_CONFIG, **merged})


def resolve_test_history_db_path(config: dict) -> Path:
    """Locate the test-history SQLite database to read.

    Resolution order (§5.6):
      (a) the ``test_history_db_path`` override if set; else
      (b) the profile-aware ``<hermes_home>/test-history/history.db`` default,
          which matches the sibling plugin's own default path.

    Option (c) from the plan — parsing ``hermes test-history status`` — is
    deliberately omitted: it would require a subprocess, and this plugin permits
    no subprocess outside ``install-cron``. When test-history uses a non-default
    location, set ``test_history_db_path`` (option a) instead.
    """
    override = config.get("test_history_db_path")
    if override:
        # expanduser/expandvars first: realpath alone leaves a leading ``~`` (or a
        # ``$VAR``) as a literal directory name, so ``~/path`` would resolve under
        # the cwd instead of $HOME.
        expanded = os.path.expanduser(os.path.expandvars(str(override)))
        return Path(os.path.realpath(expanded))
    from . import storage  # lazy: avoids import cycle

    return storage.get_hermes_home() / "test-history" / TEST_HISTORY_DB_FILENAME
