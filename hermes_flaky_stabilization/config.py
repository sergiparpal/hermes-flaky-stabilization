"""Unified configuration (plan Appendix B).

One JSON file, ``<data_dir>/config.json``, with a per-stage section for each
absorbed plugin plus the orchestrator. All keys are optional; a missing or
malformed file degrades to :data:`DEFAULTS` (never raises). Legacy environment
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
import os
import tempfile
from pathlib import Path
from typing import Any

from . import paths

CONFIG_FILE_NAME = "config.json"

DEFAULTS: dict[str, dict[str, Any]] = {
    "history": {
        "default_lookback_days": 30,
        "max_stack_trace_chars": 500,
        "db_path_override": None,
    },
    "detective": {
        "window_days": 14,
        "min_fails": 3,
        "include_errors": True,
        "schedule": "0 9 * * *",
        "deliver": "local",
        "report_scope": "changes-only",
    },
    "triage": {
        "enable_enrichment": True,
        "log_roots": None,
        "token_hosts": None,
        "allow_private": False,
    },
    "healer": {
        "burnin": "5:10",
        "sandbox": "auto",
        "docker_image": None,
        "git_tool": "terminal",
        "pr_tool": "create_pull_request",
        "base_branch": "main",
        "allow_subprocess_pr": False,
    },
    "pii": {
        "default_max_files": 2000,
    },
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
    "incidents": {
        "context_injection": True,
        "prefetch_limit": 3,
        "prefetch_timeout": 1.5,
    },
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

    ``None`` defaults accept anything (untyped/optional keys); a bool default
    must not accept ``1``/``"true"`` from JSON silently — but int/float are
    interchangeable. Type mismatches degrade to the default rather than raise.
    """
    if default is None or value is None:
        return value
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int) and not isinstance(default, bool):
        return value if isinstance(value, int) and not isinstance(value, bool) else default
    if isinstance(default, float):
        is_num = isinstance(value, (int, float)) and not isinstance(value, bool)
        return float(value) if is_num else default
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
    (forward compatibility); known keys are type-coerced against the default.
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
        if section in DEFAULTS and isinstance(values, dict):
            for key, value in values.items():
                if key in DEFAULTS[section]:
                    cfg[section][key] = _coerce(section, key, value, DEFAULTS[section][key])
                else:
                    cfg[section][key] = value
        else:
            cfg[section] = values
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
