"""The ``hermes flaky-stab`` CLI (plan §6.2, Appendix C).

One top-level command mounting the stage subcommands:

* history-owned:   ``ingest``, ``prune``, ``rebuild-fts``, ``config``
  (also re-exposed unchanged under the ``test-history`` alias — plan D2)
* detective-owned: ``scan``, ``list``, ``install-cron``
* unified:         ``status`` (this module) — later phases add ``jira`` and
  ``migrate``.

The stage argparse definitions and handlers are reused from the ported stage
``cli`` modules; this module only owns the top-level composition and the
unified ``status``. ``setup_cli`` receives an argparse subparser from the host;
``run_cli`` receives the parsed namespace (the host wires it via
``set_defaults``) and returns a process exit code.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

SUBCOMMAND_DEST = "flaky_stab_cmd"

# Subcommands delegated to each stage CLI module: name -> (module attr, handler name)
_HISTORY_COMMANDS = {
    "ingest": "_cmd_ingest",
    "prune": "_cmd_prune",
    "rebuild-fts": "_cmd_rebuild_fts",
    "config": "_cmd_config",
}
_DETECTIVE_COMMANDS = {
    "scan": "_cmd_scan",
    "list": "_cmd_list",
    "install-cron": "_cmd_install_cron",
}


def setup_cli(parser) -> None:
    import argparse

    subs = parser.add_subparsers(dest=SUBCOMMAND_DEST)

    subs.add_parser("status", help="Show data locations, schema version, and config summary")

    # -- history-owned (argument definitions mirror history.cli.setup_parser) --
    p_ingest = subs.add_parser(
        "ingest", help="Ingest a JUnit XML file or a directory of them (recursive)"
    )
    p_ingest.add_argument("path", type=str, help="Path to an .xml file or a directory")

    p_prune = subs.add_parser(
        "prune", help="Delete test runs older than --before (cascades to cases)"
    )
    p_prune.add_argument("--before", required=True, type=str, help="ISO-8601 date/time cutoff")

    subs.add_parser("rebuild-fts", help="Rebuild the test-history FTS5 index")

    subs.add_parser("config", help="Show the resolved test-history configuration")

    # -- detective-owned (argument definitions mirror detective.cli.setup_parser) --
    p_scan = subs.add_parser("scan", help="Detect flaky tests, persist verdicts, and report")
    p_scan.add_argument("--window", type=int, default=None,
                        help="Detection window in days (default: from config)")
    p_scan.add_argument("--min-fails", dest="min_fails", type=int, default=None,
                        help="Minimum failures in the window to be non-stable "
                             "(default: from config)")
    p_scan.add_argument("--include-errors", dest="include_errors",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Count `error` status as a failure (default: from config)")
    p_scan.add_argument("--format", choices=["human", "cron", "json"], default="human",
                        help="Output format (default: human)")

    p_list = subs.add_parser("list", help="List stored flaky verdicts")
    p_list.add_argument("--status", choices=["flaky", "consistently_failing", "all"],
                        default="flaky", help="Which verdicts to list (default: flaky)")

    p_cron = subs.add_parser("install-cron",
                             help="Install the nightly no-agent detection job")
    p_cron.add_argument("--schedule", default=None,
                        help="Cron expression (default: from config)")
    p_cron.add_argument("--deliver", default=None,
                        help="Delivery channel (default: from config)")
    p_cron.add_argument("--window", type=int, default=None, help="Detection window in days")
    p_cron.add_argument("--min-fails", dest="min_fails", type=int, default=None,
                        help="Minimum failures in the window to be non-stable")
    p_cron.add_argument("--no-create", dest="no_create", action="store_true",
                        help="Only write the shim + config; print the command instead "
                             "of creating the job")


def run_cli(args) -> int:
    command = getattr(args, SUBCOMMAND_DEST, None)
    if command in (None, "status"):
        print(status_text())
        return 0
    if command in _HISTORY_COMMANDS:
        from .history import cli as history_cli

        return getattr(history_cli, _HISTORY_COMMANDS[command])(args)
    if command in _DETECTIVE_COMMANDS:
        from .detective import cli as detective_cli

        return getattr(detective_cli, _DETECTIVE_COMMANDS[command])(args)
    print(f"flaky-stab: unknown subcommand {command!r}")  # unreachable via argparse
    return 2


def status_text() -> str:
    """Human-readable state summary. Never prints secret values."""
    from . import config, paths
    from .storage import state

    lines = [f"hermes-flaky-stabilization @ {paths.get_data_dir()}"]
    db_path = state.state_db_path()
    if db_path.exists():
        try:
            with closing(state.connect(db_path)) as conn:
                version = state.current_version(conn)
                fts = state.fts_available(conn)
            lines.append(f"state.db: {db_path} (schema v{version}, fts5={'yes' if fts else 'no'})")
        except sqlite3.Error as exc:
            lines.append(f"state.db: {db_path} (unreadable: {exc})")
    else:
        lines.append(f"state.db: {db_path} (not created yet)")
    cfg_path = config.config_path()
    lines.append(f"config: {cfg_path} ({'present' if cfg_path.exists() else 'defaults'})")
    return "\n".join(lines)
