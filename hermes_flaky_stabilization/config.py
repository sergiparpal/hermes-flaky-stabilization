"""Unified configuration (plan Appendix B).

One JSON file, ``<data_dir>/config.json``, with a per-stage section for each
absorbed plugin plus the orchestrator. Precedence, lowest to highest:

1. :data:`DEFAULTS` — every known key has a value here;
2. the user file, merged per key: known keys are type-coerced against their
   default (a type mismatch — including JSON ``null`` on a typed key —
   degrades to the default; a known *section* that is not a JSON object
   degrades to that section's defaults wholesale, with a warning). Unknown
   sections and unknown keys inside known sections pass through untouched
   (forward compatibility) — they are the only place non-object/mismatched
   values survive;
3. environment overrides (:data:`_ENV_OVERRIDES`), applied last so they win
   even over a degraded section.

All keys are optional; :func:`load_config` never raises — a missing or
malformed file degrades to :data:`DEFAULTS`. Legacy environment
variables keep working:

* ``FLAKY_HEALER_*`` — consumed directly by the ported healer config module
  (call sites unchanged), not here.
* ``HERMES_CI_TRIAGE_LOG_ROOTS`` / ``HERMES_CI_TRIAGE_ALLOW_PRIVATE`` and
  ``JIRA_BASE_URL`` / ``JIRA_EMAIL`` — applied here as overrides over the file
  (documented in Appendix B). Secrets (``GITHUB_TOKEN``, ``JIRA_API_TOKEN``)
  are never stored in the file and are read at call time by their stages.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import paths

logger = logging.getLogger(__name__)

CONFIG_FILE_NAME = "config.json"

DEFAULTS: dict[str, dict[str, Any]] = {
    # Read by the history stage (its storage layer merges these over its own
    # module defaults; db_path_override is validated to stay inside the
    # Hermes home).
    "history": {
        "default_lookback_days": 30,
        "max_stack_trace_chars": 500,
        "db_path_override": None,
    },
    # Read by detective/config.py (scan window/thresholds) and by the
    # install-cron composition in cli.py (schedule/deliver/report_scope).
    "detective": {
        "window_days": 14,
        "min_fails": 3,
        "include_errors": True,
        "schedule": "0 9 * * *",
        "deliver": "local",
        "report_scope": "changes-only",
    },
    # enable_enrichment gates triage's history enrichment; log_roots/
    # token_hosts/allow_private feed the triage log-fetch guardrails
    # (HERMES_CI_TRIAGE_* env vars still override — see _ENV_OVERRIDES).
    "triage": {
        "enable_enrichment": True,
        "log_roots": None,
        "token_hosts": None,
        "allow_private": False,
    },
    # Read by the healer stage config (FLAKY_HEALER_* env vars still override).
    "healer": {
        "burnin": "5:10",
        "sandbox": "auto",
        "docker_image": None,
        "git_tool": "terminal",
        "pr_tool": "create_pull_request",
        "base_branch": "main",
        "allow_subprocess_pr": False,
    },
    # default_max_files bounds validate_no_pii sweeps (orchestrator PII gate).
    "pii": {
        "default_max_files": 2000,
    },
    # Read by incidents/config.py (sync transport + local index) and by
    # incidents/write.py (enable_write / project_key / issue_type — D7).
    "jira": {
        "base_url": "",
        "email": "",
        "auth_mode": "api_token",
        "jql": "project = INC ORDER BY updated DESC",
        "root_cause_field": None,
        "page_size": 50,
        "max_pages": 20,
        "retention_days": 0,
        "sync_min_interval": 60.0,
        "enable_write": False,
        "project_key": "INC",
        "issue_type": "Bug",
    },
    # Read by the incidents service (pre_llm_call context injection + its
    # local-FTS prefetch bounds).
    "incidents": {
        "context_injection": True,
        "prefetch_limit": 3,
        "prefetch_timeout": 1.5,
    },
    # Read by the orchestrator (fork policy, heal mode, PII gate — D9).
    "pipeline": {
        "default_heal_mode": "suggest",
        "heal_categories": ["flaky", "timeout"],
        "require_pii_gate": True,
    },
}

# (section, key) -> env var that overrides the file value when set (non-empty).
_ENV_OVERRIDES: dict[tuple[str, str], str] = {
    ("jira", "base_url"): "JIRA_BASE_URL",
    ("jira", "email"): "JIRA_EMAIL",
    ("triage", "log_roots"): "HERMES_CI_TRIAGE_LOG_ROOTS",
    ("triage", "allow_private"): "HERMES_CI_TRIAGE_ALLOW_PRIVATE",
}

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def config_path(data_dir: Path | None = None) -> Path:
    return (data_dir or paths.get_data_dir()) / CONFIG_FILE_NAME


def _coerce(section: str, key: str, value: Any, default: Any) -> Any:
    """Keep a user value only when its type is compatible with the default.

    ``None`` defaults accept anything (untyped/optional keys). For typed
    defaults every mismatch degrades to the default rather than raising —
    including JSON ``null``, so e.g. ``{"detective": {"schedule": null}}``
    can never leak ``None`` into ``shlex.quote`` during install-cron. A bool
    default must not accept ``1``/``"true"`` from JSON silently, but
    int/float are interchangeable.
    """
    if default is None:
        return value
    if value is None:
        return default
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int) and not isinstance(default, bool):
        return value if isinstance(value, int) and not isinstance(value, bool) else default
    if isinstance(default, float):
        is_num = isinstance(value, (int, float)) and not isinstance(value, bool)
        numeric = float(value) if is_num else default
        return numeric if math.isfinite(numeric) else default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, list):
        return value if isinstance(value, list) else default
    return value


def _apply_env_overrides(cfg: dict[str, dict[str, Any]]) -> None:
    for (section, key), var in _ENV_OVERRIDES.items():
        raw = os.environ.get(var)
        if raw is None or raw == "":
            continue
        default = DEFAULTS[section][key]
        if isinstance(default, bool):
            cfg[section][key] = raw.strip().lower() in _TRUTHY
        else:
            cfg[section][key] = raw


def load_config(data_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """User ``config.json`` merged over :data:`DEFAULTS`, env overrides applied.

    Unknown sections and unknown keys inside known sections are preserved
    (forward compatibility); known keys are type-coerced against the default;
    a known section that is not a JSON object keeps the defaults (readers
    like ``status`` and the env-override loop index into known sections, so
    a ``null``/list section must never replace the merged dict). Never
    raises — malformed input degrades (module docstring).
    """
    cfg = copy.deepcopy(DEFAULTS)
    path = config_path(data_dir)
    user: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                user = loaded
        except Exception:
            user = {}
    for section, values in user.items():
        if section not in DEFAULTS:
            # Unknown sections pass through untouched (forward compatibility);
            # only they may hold non-object values.
            cfg[section] = values
            continue
        if not isinstance(values, dict):
            logger.warning(
                "config.json: known section %r is not a JSON object (got %s); "
                "keeping defaults", section, type(values).__name__)
            continue
        for key, value in values.items():
            if key in DEFAULTS[section]:
                cfg[section][key] = _coerce(section, key, value, DEFAULTS[section][key])
            else:
                cfg[section][key] = value
    _apply_env_overrides(cfg)
    return cfg


def write_config(cfg: dict[str, Any], data_dir: Path | None = None) -> Path:
    """Atomically persist *cfg* with owner-only permissions."""
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


@contextmanager
def _config_lock(data_dir: Path | None = None):
    """Cross-process advisory lock for read-modify-write config updates."""
    lock_path = config_path(data_dir).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield


def update_section(section: str, updates: dict[str, Any],
                   data_dir: Path | None = None) -> dict[str, Any]:
    """Atomically merge one section without losing concurrent section writes."""
    with _config_lock(data_dir):
        path = config_path(data_dir)
        whole: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                whole = loaded
        except (OSError, ValueError, TypeError):
            pass
        current = whole.get(section)
        merged = {**(current if isinstance(current, dict) else {}), **updates}
        whole[section] = merged
        write_config(whole, data_dir)
        return merged
