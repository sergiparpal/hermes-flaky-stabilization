"""The ``hermes flaky-stab`` CLI.

Phase 1 ships the ``status`` subcommand; the stage subcommands (``ingest``,
``scan``, ``jira``, ``migrate``, ``install-cron``, …) land with their stages.
``setup_cli`` receives an argparse subparser from the host; ``run_cli``
receives the parsed namespace (the host wires it via ``set_defaults``).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

SUBCOMMAND_DEST = "flaky_stab_cmd"


def setup_cli(parser) -> None:
    sub = parser.add_subparsers(dest=SUBCOMMAND_DEST)
    sub.add_parser(
        "status", help="Show data locations, schema version, and config summary"
    )


def run_cli(args) -> None:
    command = getattr(args, SUBCOMMAND_DEST, None)
    if command in (None, "status"):
        print(status_text())
    else:  # argparse enforces choices; keep a safe fallback anyway
        print(f"flaky-stab: unknown subcommand {command!r}")


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
