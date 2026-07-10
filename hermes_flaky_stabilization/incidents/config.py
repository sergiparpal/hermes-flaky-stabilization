"""Shared, Hermes-free plumbing for the hermes-jira-incidents plugin.

Both the provider (``__init__.py``) and the CLI (``cli.py``) need the same
config/path resolution and Jira-client construction. The CLI is imported by
Hermes at argparse time — *before* the provider and its heavy SDK imports — so
``cli.py`` must not import ``__init__``. To keep that constraint without
duplicating the logic in two places (where the copies had already drifted, e.g.
``HERMES_HOME`` was honoured in the CLI but not in the provider's fallback),
the shared, dependency-light helpers live here.

This module imports only the standard library plus the plugin's own
stdlib-only :mod:`jira_client`; it never imports Hermes, so it is safe to load
from the lightweight CLI path.

:class:`IncidentsConfig` is the **single source of truth** for the plugin's
tunable defaults, their coercion from the raw JSON dict, and their clamping to
sane ranges. Previously these defaults lived in three places (module constants
in ``__init__.py``, the ``DEFAULT_*`` constants here, and literal signature
defaults in :func:`ingest.run_sync`); parsing happened ad hoc at every read
site. Parsing once into a typed, frozen object removes that duplication and
makes every read site type-safe.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from . import jira_client as jira_mod

logger = logging.getLogger(__name__)

# -- identity / filenames ----------------------------------------------------

NAME = "hermes-jira-incidents"
ENV_TOKEN = "JIRA_API_TOKEN"
CONFIG_FILE = "hermes-jira-incidents.json"
DB_FILE = "hermes-jira-incidents.db"
DEFAULT_JQL = "project = INC ORDER BY updated DESC"

# Config keys that hold (or could accidentally hold) the Jira API token. They
# are never written to the JSON config or printed — the token lives only in the
# ``JIRA_API_TOKEN`` env var. Single source of truth for both the provider's
# save_config() strip and the CLI's `config` redaction.
SECRET_CONFIG_KEYS = ("api_token", ENV_TOKEN, "token")

# Default sync/recall knobs. These are the field defaults of IncidentsConfig
# (referenced below) and remain importable for any caller that wants the bare
# value. They are the app-level source of truth; ingest.run_sync keeps its own
# matching signature defaults only because it is deliberately import-free so it
# unit-tests with no plugin dependencies — real callers always pass the values
# resolved here via IncidentsConfig.run_sync_kwargs().
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 20
DEFAULT_PREFETCH_LIMIT = 3
DEFAULT_SEARCH_LIMIT = 5
DEFAULT_PREFETCH_TIMEOUT = 1.5
DEFAULT_SYNC_MIN_INTERVAL = 60.0


# -- coercion helpers (lenient: never raise; log + fall back per field) -------
#
# The provider runs these on the turn's hot path, where the project's HARD rule
# is "never break the turn loop". So coercion degrades to the field default and
# logs a warning rather than raising — bad config is surfaced loudly at load
# (the warning names the field and value) without ever taking the agent down.

def _as_int(value: Any, default: int, *, lo: int | None = None,
            hi: int | None = None, label: str = "") -> int:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        logger.warning("%s: invalid %s=%r; using default %r", NAME, label, value, default)
        return default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _as_float(value: Any, default: float, *, lo: float | None = None,
              hi: float | None = None, label: str = "") -> float:
    if value is None or value == "":
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        logger.warning("%s: invalid %s=%r; using default %r", NAME, label, value, default)
        return default
    if lo is not None and f < lo:
        f = lo
    if hi is not None and f > hi:
        f = hi
    return f


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _as_str_tuple(value: Any) -> tuple[str, ...] | None:
    """Coerce a JSON list (or comma string) of field names into a tuple, or None."""
    if not value:
        return None
    if isinstance(value, str):
        items = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(p).strip() for p in value]
    else:
        return None
    items = [p for p in items if p]
    return tuple(items) or None


@dataclass(frozen=True)
class IncidentsConfig:
    """Typed, validated view of the plugin's non-secret configuration.

    Build it with :meth:`from_dict` (parsing the JSON config). All numeric
    fields are coerced and clamped to sane ranges there, so every read site can
    treat them as already-valid primitives. The Jira API token is intentionally
    **not** a field here — it is read from the env var only (see
    :func:`resolve_token`).
    """

    # connection / auth
    jira_base_url: str = ""
    jira_email: str = ""
    auth_mode: str = "api_token"
    timeout: float = jira_mod.DEFAULT_TIMEOUT
    # query / mapping
    jql: str = DEFAULT_JQL
    root_cause_field: str = ""
    fields: tuple[str, ...] | None = None
    # background sync
    page_size: int = DEFAULT_PAGE_SIZE
    max_pages: int = DEFAULT_MAX_PAGES
    retention_days: int = 0
    sync_min_interval: float = DEFAULT_SYNC_MIN_INTERVAL
    # recall
    prefetch_limit: int = DEFAULT_PREFETCH_LIMIT
    prefetch_timeout: float = DEFAULT_PREFETCH_TIMEOUT
    # storage ("" => derive <hermes_home>/DB_FILE)
    db_path: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> IncidentsConfig:
        """Parse a raw config dict into a validated IncidentsConfig.

        Unknown keys are ignored; missing keys take the field default; bad
        values fall back to the default with a logged warning (never raises).
        """
        d = raw or {}
        return cls(
            jira_base_url=_as_str(d.get("jira_base_url")),
            jira_email=_as_str(d.get("jira_email")),
            auth_mode=_as_str(d.get("auth_mode"), "api_token") or "api_token",
            timeout=_as_float(d.get("timeout"), jira_mod.DEFAULT_TIMEOUT,
                              lo=1.0, hi=300.0, label="timeout"),
            jql=_as_str(d.get("jql")) or DEFAULT_JQL,
            root_cause_field=_as_str(d.get("root_cause_field")),
            fields=_as_str_tuple(d.get("fields")),
            page_size=_as_int(d.get("page_size"), DEFAULT_PAGE_SIZE,
                              lo=1, hi=100, label="page_size"),
            max_pages=_as_int(d.get("max_pages"), DEFAULT_MAX_PAGES,
                              lo=1, hi=1000, label="max_pages"),
            retention_days=_as_int(d.get("retention_days"), 0, lo=0, label="retention_days"),
            sync_min_interval=_as_float(d.get("sync_min_interval"), DEFAULT_SYNC_MIN_INTERVAL,
                                        lo=0.0, hi=86400.0, label="sync_min_interval"),
            prefetch_limit=_as_int(d.get("prefetch_limit"), DEFAULT_PREFETCH_LIMIT,
                                   lo=1, hi=50, label="prefetch_limit"),
            prefetch_timeout=_as_float(d.get("prefetch_timeout"), DEFAULT_PREFETCH_TIMEOUT,
                                       lo=0.1, hi=60.0, label="prefetch_timeout"),
            db_path=_as_str(d.get("db_path")),
        )

    # -- derived accessors (env fallbacks resolved at call time) -------------

    def base_url(self) -> str:
        """Configured base URL, falling back to the ``JIRA_BASE_URL`` env var."""
        return self.jira_base_url or os.environ.get("JIRA_BASE_URL") or ""

    def email(self) -> str:
        """Configured account email, falling back to the ``JIRA_EMAIL`` env var."""
        return self.jira_email or os.environ.get("JIRA_EMAIL") or ""

    def effective_db_path(self, hermes_home: str) -> str:
        """Resolve the SQLite index path.

        Expands ``$HERMES_HOME``/``${HERMES_HOME}`` placeholders and a leading
        ``~`` so a configured ``db_path`` like ``~/jira/index.db`` lands in the
        home directory rather than a literal ``~`` folder.
        """
        db_path = self.db_path or os.path.join(hermes_home, DB_FILE)
        db_path = db_path.replace("$HERMES_HOME", hermes_home).replace(
            "${HERMES_HOME}", hermes_home)
        return os.path.expanduser(db_path)

    def run_sync_kwargs(self) -> dict[str, Any]:
        """Build the keyword args for :func:`ingest.run_sync`."""
        fields = list(self.fields) if self.fields else None
        if self.root_cause_field:
            # The configured root-cause field must actually be requested from
            # Jira, or extract_root_cause can never see it. When `fields` is
            # unset the client would fall back to DEFAULT_SEARCH_FIELDS, which
            # cannot include a per-site custom field — materialise the default
            # list and append. (Without this, root_cause_field was a silent
            # no-op and jira_get_root_cause fell back to the description.)
            if fields is None:
                fields = list(jira_mod.DEFAULT_SEARCH_FIELDS)
            if self.root_cause_field not in fields:
                fields.append(self.root_cause_field)
        return {
            "fields": fields,
            "root_cause_field": self.root_cause_field or None,
            "page_size": self.page_size,
            "max_pages": self.max_pages,
        }


# -- config / path resolution ------------------------------------------------

def resolve_hermes_home() -> str:
    """Resolve the active Hermes home directory (delegates to the unified resolver).

    Used only outside the normal ``initialize(hermes_home=...)`` path (the CLI
    and ``is_available``). ``paths`` imports Hermes lazily *inside* the call, so
    importing it here does not break this module's "safe to load from the
    lightweight CLI path" constraint.

    The local lookup chain this replaced skipped ``$HERMES_HOME`` expansion, so
    ``HERMES_HOME=~/hermes-data`` indexed incidents under a literal ``./~``.
    """
    from .. import paths

    return str(paths.get_hermes_home())


def load_config(hermes_home: str) -> dict[str, Any]:
    """Load the raw non-secret config dict for :meth:`IncidentsConfig.from_dict`.

    Unified-plugin adaptation (plan Appendix B/C): the legacy
    ``<hermes_home>/hermes-jira-incidents.json`` is replaced by the ``jira`` and
    ``incidents`` sections of the unified
    ``<hermes_home>/flaky-stabilization/config.json``; this function maps those
    sections onto the legacy raw-key dict so :class:`IncidentsConfig` (and every
    caller) stays verbatim. ``db_path`` is intentionally not mapped — incident
    storage is consolidated into ``state.db`` (plan D4); an explicit constructor
    override (tests) still wins inside the service. Token-ish keys are never
    read from the file (the token lives only in the env var).
    """
    try:
        from pathlib import Path

        from .. import config as unified_config

        cfg = unified_config.load_config(Path(hermes_home) / "flaky-stabilization")
    except Exception as e:
        logger.warning("%s: failed to read unified config: %s", NAME, e)
        return {}
    jira = cfg.get("jira") if isinstance(cfg.get("jira"), dict) else {}
    inc = cfg.get("incidents") if isinstance(cfg.get("incidents"), dict) else {}
    return {
        "jira_base_url": jira.get("base_url", ""),
        "jira_email": jira.get("email", ""),
        "auth_mode": jira.get("auth_mode", "api_token"),
        "timeout": jira.get("timeout"),
        "jql": jira.get("jql"),
        "root_cause_field": jira.get("root_cause_field") or "",
        "fields": jira.get("fields"),
        "page_size": jira.get("page_size"),
        "max_pages": jira.get("max_pages"),
        "retention_days": jira.get("retention_days"),
        "sync_min_interval": jira.get("sync_min_interval"),
        "prefetch_limit": inc.get("prefetch_limit"),
        "prefetch_timeout": inc.get("prefetch_timeout"),
    }


# -- credential resolution ---------------------------------------------------

def resolve_token() -> str | None:
    """Read the Jira API token from its env var (never from disk)."""
    return os.environ.get(ENV_TOKEN)


# -- Jira client construction ------------------------------------------------

def build_client(cfg: IncidentsConfig, *, token: str) -> jira_mod.JiraClient:
    """Construct a :class:`JiraClient` from *cfg* + the resolved token.

    Centralises the constructor-argument wiring shared by the provider's
    background sync and the CLI ``sync`` command. May raise ``ValueError`` (e.g.
    a non-https base URL); callers decide how to surface that.
    """
    return jira_mod.JiraClient(
        base_url=cfg.base_url(),
        auth_mode=cfg.auth_mode,
        email=cfg.email(),
        token=token,
        timeout=cfg.timeout,
    )
