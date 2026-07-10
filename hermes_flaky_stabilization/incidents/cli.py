"""Incidents CLI, re-homed under ``hermes flaky-stab jira <status|sync|config>``
(plan Appendix C; the legacy top-level command belonged to the memory slot).

This module is intentionally lightweight — the host imports CLI modules during
argparse setup — and depends only on the stage's own stdlib-based modules.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from ..pii import redaction
from . import config as config_mod
from . import ingest as ingest_mod
from .jira_client import JiraError
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
    p_sync = actions.add_parser("sync", help="Reindex from Jira now (synchronous)")
    p_sync.add_argument(
        "--full", action="store_true",
        help="Re-ingest everything from scratch: clears the sync watermark and "
             "resume state, then — after a run that completed without hitting "
             "max_pages — deletes local incidents Jira no longer returns "
             "(issues deleted or re-keyed upstream). Deletion is skipped if "
             "the run was truncated.")
    p_sync.add_argument(
        "--quiet", action="store_true",
        help="Suppress the success line when nothing changed "
             "(errors still go to stderr).")
    actions.add_parser("config", help="Show effective (non-secret) config")


def hermes_jira_incidents_command(args) -> int:
    """Dispatch a ``flaky-stab jira`` action. Returns a process exit code
    (0 success, 1 credential/config/Jira failure) for the top-level
    ``run_cli`` to propagate — the nightly cron shim alerts on non-zero."""
    action = getattr(args, "action", None) or "status"
    home = _hermes_home()
    raw = config_mod.load_config(home)
    if action == "sync":
        return _cmd_sync(home, raw,
                         full=bool(getattr(args, "full", False)),
                         quiet=bool(getattr(args, "quiet", False)))
    if action == "config":
        return _cmd_config(raw)
    return _cmd_status(home, raw)


def _db_path(home: str, cfg: config_mod.IncidentsConfig) -> str:
    """Consolidated storage (plan D4): state.db unless db_path is overridden."""
    if cfg.db_path:
        return cfg.effective_db_path(home)
    return os.path.join(home, "flaky-stabilization", "state.db")


def _cmd_status(home: str, raw: dict[str, Any]) -> int:
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
    return 0


def _cmd_config(raw: dict[str, Any]) -> int:
    # Show the raw on-disk config (arbitrary keys), minus secrets. Defensive:
    # never print secrets even if a token leaked into the JSON.
    safe = {k: v for k, v in raw.items() if k not in config_mod.SECRET_CONFIG_KEYS}
    safe = {k: (redaction.redact_text(v) if isinstance(v, str) else v) for k, v in safe.items()}
    print(json.dumps(safe, indent=2, sort_keys=True))
    return 0


def _cmd_sync(home: str, raw: dict[str, Any], *, full: bool = False,
              quiet: bool = False) -> int:
    """Run one synchronous sync. Returns 0 on success, 1 on failure.

    ``full`` clears the watermark/backfill state, re-ingests everything and —
    only when the run completed (was not truncated by max_pages) — deletes
    local incidents that Jira no longer returned. ``quiet`` suppresses the
    success line when nothing changed (cron shims want silent ticks); errors
    always go to stderr.
    """
    cfg = config_mod.IncidentsConfig.from_dict(raw)
    token = config_mod.resolve_token()
    if not token or not cfg.base_url():
        print("Cannot sync: set JIRA_API_TOKEN in .env and jira_base_url in config.",
              file=sys.stderr)
        return 1
    try:
        client = config_mod.build_client(cfg, token=token)
    except ValueError as e:
        # e.g. a non-https base_url, which we refuse so the token is never sent
        # over cleartext.
        print(f"Cannot sync: {e}", file=sys.stderr)
        return 1
    store = IncidentStore(_db_path(home, cfg))
    try:
        if full:
            # Restart ingest from scratch so the run enumerates every live
            # issue (needed to detect deletions/re-keys).
            ingest_mod.reset_sync_state(store)
        try:
            result = ingest_mod.run_sync(
                store, client, cfg.jql, incremental=not full,
                **cfg.run_sync_kwargs(),
            )
        except JiraError as e:
            # Clean one-line failure (no traceback) with a non-zero exit code,
            # so the nightly cron shim can alert.
            print(f"Sync failed: {e}", file=sys.stderr)
            return 1
        removed = _reconcile_full_sync(store, result) if full else 0
        changed = bool(result["ingested"] or removed)
        incomplete = not result.get("backfill_complete", True)
        # Report unless this is a quiet cron tick with nothing to say; a quiet run
        # still speaks up when something changed or the backfill is unfinished.
        should_report = changed or incomplete or not quiet
        if should_report:
            print(f"Sync complete: ingested {result['ingested']} incident(s) "
                  f"over {result['pages']} page(s). Watermark: {result['watermark']}")
            if removed:
                print(f"Removed {removed} local incident(s) no longer present in Jira.")
        if incomplete and not full:  # the full path printed its own notice
            print("Initial backfill is not finished yet — run sync again to "
                  "continue indexing older incidents.")
        return 0
    finally:
        store.close()


def _reconcile_full_sync(store: IncidentStore, result: dict[str, Any]) -> int:
    """After a ``--full`` run, delete local incidents Jira no longer returns.

    Deletion is safe ONLY when the run enumerated every live issue (was not
    truncated by ``max_pages``); a truncated run has not seen the tail, so
    dropping "unseen" locals would be false deletions. Returns the count
    removed, and prints its own skip notice when it cannot safely reconcile.
    """
    if result.get("backfill_complete"):
        return store.delete_keys_not_in(result.get("seen_keys") or set())
    print("Full sync was truncated by max_pages before seeing every "
          "issue — skipping deletion of unseen local incidents; "
          "run `jira sync` again to finish re-indexing.")
    return 0

