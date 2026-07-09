"""Incidents CLI, re-homed under ``hermes flaky-stab jira <status|sync|config>``
(plan Appendix C; the legacy top-level command belonged to the memory slot).

This module is intentionally lightweight — the host imports CLI modules during
argparse setup — and depends only on the stage's own stdlib-based modules.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..pii import redaction
from . import config as config_mod
from . import ingest as ingest_mod
from .store import IncidentStore


def _hermes_home() -> str:
    """Thin module-level seam over :func:`config.resolve_hermes_home`.

    Kept as its own function (rather than calling config directly at each call
    site) so tests can monkeypatch ``cli._hermes_home`` to point at a temp home.
    """
    return config_mod.resolve_hermes_home()


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def register_cli(subparser) -> None:
    """Attach the ``jira`` subcommands to *subparser* (a `flaky-stab jira` node)."""
    actions = subparser.add_subparsers(dest="action", metavar="{status,sync,config}")
    actions.add_parser("status", help="Show config + local index stats")
    actions.add_parser("sync", help="Reindex from Jira now (synchronous)")
    actions.add_parser("config", help="Show effective (non-secret) config")


def hermes_jira_incidents_command(args) -> None:
    action = getattr(args, "action", None) or "status"
    home = _hermes_home()
    raw = config_mod.load_config(home)
    if action == "sync":
        _cmd_sync(home, raw)
    elif action == "config":
        _cmd_config(raw)
    else:
        _cmd_status(home, raw)


def _db_path(home: str, cfg: config_mod.IncidentsConfig) -> str:
    """Consolidated storage (plan D4): state.db unless db_path is overridden."""
    if cfg.db_path:
        return cfg.effective_db_path(home)
    return os.path.join(home, "flaky-stabilization", "state.db")


def _cmd_status(home: str, raw: dict[str, Any]) -> None:
    cfg = config_mod.IncidentsConfig.from_dict(raw)
    db_path = _db_path(home, cfg)
    print("jira incidents — status")
    print(f"  hermes_home : {home}")
    print(f"  base_url    : {cfg.base_url() or '(unset)'}")
    print(f"  auth_mode   : {cfg.auth_mode}")
    print(f"  token set   : {bool(config_mod.resolve_token())}")  # boolean only — never the token
    print(f"  jql         : {cfg.jql}")
    if os.path.exists(db_path):
        store = IncidentStore(db_path)
        try:
            stats = store.stats()
            print(f"  db_path     : {stats['db_path']}")
            print(f"  incidents   : {stats['incidents']}")
            print(f"  links       : {stats['links']}")
            print(f"  watermark   : {stats['watermark'] or '(none)'}")
        finally:
            store.close()
    else:
        print(f"  db_path     : {db_path} (not created yet)")


def _cmd_config(raw: dict[str, Any]) -> None:
    # Show the raw on-disk config (arbitrary keys), minus secrets. Defensive:
    # never print secrets even if a token leaked into the JSON.
    safe = {k: v for k, v in raw.items() if k not in config_mod.SECRET_CONFIG_KEYS}
    safe = {k: (redaction.redact_text(v) if isinstance(v, str) else v) for k, v in safe.items()}
    print(json.dumps(safe, indent=2, sort_keys=True))


def _cmd_sync(home: str, raw: dict[str, Any]) -> None:
    cfg = config_mod.IncidentsConfig.from_dict(raw)
    token = config_mod.resolve_token()
    if not token or not cfg.base_url():
        print("Cannot sync: set JIRA_API_TOKEN in .env and jira_base_url in config.")
        return
    try:
        client = config_mod.build_client(cfg, token=token)
    except ValueError as e:
        # e.g. a non-https base_url, which we refuse so the token is never sent
        # over cleartext.
        print(f"Cannot sync: {e}")
        return
    store = IncidentStore(_db_path(home, cfg))
    try:
        result = ingest_mod.run_sync(
            store, client, cfg.jql, **cfg.run_sync_kwargs(),
        )
        print(f"Sync complete: ingested {result['ingested']} incident(s) "
              f"over {result['pages']} page(s). Watermark: {result['watermark']}")
        if not result.get("backfill_complete", True):
            print("Initial backfill is not finished yet — run sync again to "
                  "continue indexing older incidents.")
    finally:
        store.close()

