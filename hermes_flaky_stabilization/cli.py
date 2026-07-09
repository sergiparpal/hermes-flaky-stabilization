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
from pathlib import Path

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
    p_cron.add_argument("--with-jira-sync", dest="with_jira_sync", action="store_true",
                        help="Also install a second no-agent job that runs "
                             "`hermes flaky-stab jira sync` on the same schedule")

    # -- incidents-owned (argument definitions from incidents.cli) --
    from .incidents import cli as incidents_cli

    p_jira = subs.add_parser("jira", help="Manage the local Jira incident index")
    incidents_cli.register_cli(p_jira)

    # -- unified-owned: legacy data migration (plan Phase 7 / §10) --
    p_migrate = subs.add_parser(
        "migrate", help="Copy data from the four legacy plugin DBs into state.db"
    )
    p_migrate.add_argument("--dry-run", dest="dry_run", action="store_true",
                           help="Report what would be copied; write nothing")
    p_migrate.add_argument("--healer-db", dest="healer_db", default=None,
                           help="Override the legacy healer.db path (e.g. a "
                                "relocated FLAKY_HEALER_DATA_DIR)")


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

        rc = getattr(detective_cli, _DETECTIVE_COMMANDS[command])(args)
        if command == "install-cron" and rc == 0 and getattr(args, "with_jira_sync", False):
            rc = _install_jira_sync_job(args)
        return rc
    if command == "jira":
        from .incidents import cli as incidents_cli

        incidents_cli.hermes_jira_incidents_command(args)
        return 0
    if command == "migrate":
        return _cmd_migrate(args)
    print(f"flaky-stab: unknown subcommand {command!r}")  # unreachable via argparse
    return 2


def _cmd_migrate(args) -> int:
    from .storage import migrate_legacy

    overrides = {}
    if getattr(args, "healer_db", None):
        from pathlib import Path

        overrides["flaky_healer"] = Path(args.healer_db)
    reports = migrate_legacy.migrate(dry_run=args.dry_run, overrides=overrides)
    for report in reports:
        if not report.found:
            print(f"{report.name}: {report.path} (not found — skipped)")
            continue
        if report.skipped and not report.tables:
            print(f"{report.name}: {report.path} ({report.skipped})")
            continue
        detail = ", ".join(f"{table}={count}" for table, count in report.tables.items())
        verb = "would copy" if args.dry_run else "copied"
        print(f"{report.name}: {report.path} — {verb} {detail or 'nothing'}")
    if args.dry_run:
        print("dry-run: nothing was written")
    else:
        print("migration complete (sources untouched; re-running adds nothing)")
    return 0


JIRA_SYNC_SHIM = "flaky-stab-jira-sync.sh"
JIRA_SYNC_JOB = "flaky-stabilization-jira-sync"


def _install_jira_sync_job(args) -> int:
    """Optionally install the second no-agent job (plan D10 --with-jira-sync)."""
    import shlex
    import shutil
    import subprocess

    from . import config as unified_config
    from . import paths

    cfg = unified_config.load_config()["detective"]
    schedule = args.schedule or cfg["schedule"]
    deliver = args.deliver or cfg["deliver"]

    scripts_dir = paths.get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shim_src = Path(__file__).resolve().parents[1] / "scripts" / JIRA_SYNC_SHIM
    shim_dst = scripts_dir / JIRA_SYNC_SHIM
    shutil.copyfile(shim_src, shim_dst)
    try:
        import os

        os.chmod(shim_dst, 0o700)
    except OSError:
        pass
    print(f"installed shim: {shim_dst} (mode 0700)")

    printable = (f"hermes cron create {shlex.quote(schedule)} --no-agent "
                 f"--script {JIRA_SYNC_SHIM} --deliver {shlex.quote(deliver)} "
                 f"--name {JIRA_SYNC_JOB}")
    if getattr(args, "no_create", False):
        print("skipping jira-sync job creation (--no-create). To create it later, run:")
        print(f"  {printable}")
        return 0
    cron_cmd = ["hermes", "cron", "create", schedule, "--no-agent",
                "--script", JIRA_SYNC_SHIM, "--deliver", deliver,
                "--name", JIRA_SYNC_JOB]
    try:
        result = subprocess.run(cron_cmd, capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        print(f"could not run the hermes CLI ({exc}). To create the job, run:")
        print(f"  {printable}")
        return 0
    if result.returncode == 0:
        print(f"created cron job '{JIRA_SYNC_JOB}' (verify with `hermes cron list`).")
        return 0
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        print(detail)
    print("could not create the jira-sync job automatically. To create it, run:")
    print(f"  {printable}")
    return 0


_STATUS_TABLES = (
    "flaky_verdicts", "scan_runs", "triage_patterns", "healer_runs", "recipes",
    "audit", "incidents", "links", "meta", "pipeline_runs",
)


def status_text() -> str:
    """Human-readable state summary (plan Phase 7): DB paths, schema versions,
    per-table counts, config summary, credential presence — booleans only,
    never token values."""
    import json as _json
    import os

    from . import config, paths
    from .storage import state

    lines = [f"hermes-flaky-stabilization @ {paths.get_data_dir()}"]

    # -- state.db ---------------------------------------------------------
    db_path = state.state_db_path()
    if db_path.exists():
        try:
            with closing(state.connect(db_path)) as conn:
                version = state.current_version(conn)
                fts = state.fts_available(conn)
                counts = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in _STATUS_TABLES
                }
            lines.append(
                f"state.db:   {db_path} (schema v{version}, "
                f"fts5={'yes' if fts else 'no'})"
            )
            lines.append("counts:     " + ", ".join(
                f"{table}={count}" for table, count in counts.items()))
        except sqlite3.Error as exc:
            lines.append(f"state.db:   {db_path} (unreadable: {exc})")
    else:
        lines.append(f"state.db:   {db_path} (not created yet)")

    # -- history.db (the keystone public contract, D2) -----------------------
    history_db = paths.get_hermes_home() / "test-history" / "history.db"
    if history_db.exists():
        try:
            with closing(sqlite3.connect(f"file:{history_db}?mode=ro", uri=True)) as conn:
                runs = conn.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0]
                cases = conn.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0]
                hv = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
            lines.append(f"history.db: {history_db} (schema v{hv}, "
                         f"runs={runs}, cases={cases})")
        except sqlite3.Error as exc:
            lines.append(f"history.db: {history_db} (unreadable: {exc})")
    else:
        lines.append(f"history.db: {history_db} (not created yet)")

    # -- config + credentials (presence booleans only) ------------------------
    cfg_path = config.config_path()
    lines.append(f"config:     {cfg_path} "
                 f"({'present' if cfg_path.exists() else 'defaults'})")
    cfg = config.load_config()
    summary = {
        "detective": {k: cfg["detective"][k] for k in ("window_days", "min_fails",
                                                       "schedule")},
        "pipeline": cfg["pipeline"],
        "jira": {"base_url": cfg["jira"]["base_url"],
                 "enable_write": cfg["jira"]["enable_write"]},
        "incidents": cfg["incidents"],
    }
    lines.append("resolved config (summary):")
    lines.append(_json.dumps(summary, indent=2, sort_keys=True))
    lines.append(f"GITHUB_TOKEN set:   {bool(os.environ.get('GITHUB_TOKEN'))}")
    lines.append(f"JIRA_API_TOKEN set: {bool(os.environ.get('JIRA_API_TOKEN'))}")
    return "\n".join(lines)
