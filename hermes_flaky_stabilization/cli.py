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
import sys
from contextlib import closing

from . import cronjobs

SUBCOMMAND_DEST = "flaky_stab_cmd"

# Subcommands mounted from each stage CLI. The handler for each comes from that
# module's own `command_handlers()` table — this module names only the SUBSET it
# re-exposes (the stage `status` commands are shadowed by the unified one below).
_HISTORY_COMMANDS = ("ingest", "prune", "rebuild-fts", "config")
_DETECTIVE_COMMANDS = ("scan", "list", "install-cron")


def setup_cli(parser) -> None:
    from .detective import cli as detective_cli
    from .history import cli as history_cli
    from .incidents import cli as incidents_cli

    subs = parser.add_subparsers(dest=SUBCOMMAND_DEST)

    subs.add_parser("status", help="Show data locations, schema version, and config summary")

    # Arguments come from the owning stage's arg-adders; only the `help=` text is
    # local, because it names this namespace ("the test-history FTS5 index").
    # -- history-owned --
    history_cli.add_ingest_args(subs.add_parser(
        "ingest", help="Ingest a JUnit XML file or a directory of them (recursive)"))
    history_cli.add_prune_args(subs.add_parser(
        "prune", help="Delete test runs older than --before (cascades to cases)"))
    subs.add_parser("rebuild-fts", help="Rebuild the test-history FTS5 index")
    subs.add_parser("config", help="Show the resolved test-history configuration")

    # -- detective-owned --
    detective_cli.add_scan_args(subs.add_parser(
        "scan", help="Detect flaky tests, persist verdicts, and report"))
    detective_cli.add_list_args(subs.add_parser("list", help="List stored flaky verdicts"))

    p_cron = subs.add_parser("install-cron",
                             help="Install the nightly no-agent detection job")
    detective_cli.add_install_cron_args(p_cron)
    # Unified-only: the detective CLI has no Jira sync to install alongside.
    p_cron.add_argument("--with-jira-sync", dest="with_jira_sync", action="store_true",
                        help="Also install a second no-agent job that runs "
                             "`hermes flaky-stab jira sync` on the same schedule")

    # -- incidents-owned (argument definitions from incidents.cli) --
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

        return history_cli.command_handlers()[command](args)
    if command in _DETECTIVE_COMMANDS:
        from .detective import cli as detective_cli

        rc = detective_cli.command_handlers()[command](args)
        if command == "install-cron" and rc == 0 and getattr(args, "with_jira_sync", False):
            rc = _install_jira_sync_job(args)
        return rc
    if command == "jira":
        from .incidents import cli as incidents_cli

        # AGREED CONTRACT with the incidents CLI: it returns an int exit code
        # (0 success, 1 credential/config/JiraError failure). Propagate it so
        # handled sync failures exit non-zero and the nightly cron shim can
        # alert; tolerate None (treated as 0) so the seam stays robust.
        rc = incidents_cli.hermes_jira_incidents_command(args)
        return 0 if rc is None else rc
    if command == "migrate":
        return _cmd_migrate(args)
    # Unreachable via argparse. stderr, like every other error: stdout is the
    # nightly cron job's delivery payload (see _cmd_migrate).
    print(f"flaky-stab: unknown subcommand {command!r}", file=sys.stderr)
    return 2


def _cmd_migrate(args) -> int:
    from .storage import migrate_legacy

    overrides = {}
    if getattr(args, "healer_db", None):
        from pathlib import Path

        overrides["flaky_healer"] = Path(args.healer_db)
    try:
        reports = migrate_legacy.migrate(dry_run=args.dry_run, overrides=overrides)
    except migrate_legacy.MigrationError as exc:
        # e.g. a read-only ATTACH the SQLite build refused — abort loudly
        # rather than risk touching a legacy source (D4 rollback promise).
        #
        # stderr, not stdout: a no-agent cron job delivers the shim's stdout
        # VERBATIM as its report (`exec hermes flaky-stab scan --format cron`),
        # so an error printed there is delivered as though it were the report.
        # Every CLI in this plugin now sends `error:`/abort lines to stderr and
        # signals failure through the exit code, which is what makes the job
        # alert. Errors -> stderr, reports -> stdout.
        print(f"migration aborted: {exc}", file=sys.stderr)
        return 1
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


def _jira_sync_job() -> cronjobs.CronJob:
    """This job's identity, read from the globals at CALL time (mirrors
    ``detective.cli._scan_job``, which tests monkeypatch)."""
    return cronjobs.CronJob(JIRA_SYNC_SHIM, JIRA_SYNC_JOB, "jira-sync job")


def _install_jira_sync_job(args) -> int:
    """Optionally install the second no-agent job (plan D10 --with-jira-sync).

    Shares ``cronjobs`` with the detection job's installer. This function used to
    re-inline that recipe and had drifted: it never chmod'd ``scripts/`` to 0700,
    and it copied the shim with neither an existence check nor an ``OSError``
    guard — so a site-packages install raised an uncaught FileNotFoundError.
    """
    from . import config as unified_config
    from .detective import domain

    cfg = unified_config.load_config()["detective"]
    schedule = args.schedule or cfg["schedule"]
    deliver = args.deliver or cfg["deliver"]

    # The schedule is passed positionally to `hermes cron create`; reject a
    # leading-dash / malformed value that could be parsed as an option (argument
    # injection) before it is baked into the job.
    try:
        domain.validate_cron_schedule(schedule)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    job = _jira_sync_job()
    problem = cronjobs.missing_shim_error(job.shim_name)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    try:
        shim_dst = cronjobs.install_shim(job.shim_name)
    except OSError as exc:
        print(f"error: could not install the cron shim into <hermes_home>/scripts/: {exc}",
              file=sys.stderr)
        return 1
    print(f"installed shim: {shim_dst} (mode 0700)")

    if getattr(args, "no_create", False):
        print(f"skipping {job.description} creation (--no-create). To create it later, run:")
        cronjobs.print_manual_command(cronjobs.printable_command(job, schedule, deliver))
        return 0
    return cronjobs.create_job(job, schedule, deliver)


_STATUS_TABLES = (
    "flaky_verdicts", "scan_runs", "triage_patterns", "healer_runs", "recipes",
    "audit", "incidents", "links", "meta", "pipeline_runs",
)


def _state_db_lines() -> list[str]:
    """Schema version, FTS availability, and per-table counts for state.db."""
    from .storage import state

    db_path = state.state_db_path()
    if not db_path.exists():
        return [f"state.db:   {db_path} (not created yet)"]
    try:
        with closing(state.connect(db_path)) as conn:
            version = state.current_version(conn)
            fts = state.fts_available(conn)
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in _STATUS_TABLES
            }
    except sqlite3.Error as exc:
        return [f"state.db:   {db_path} (unreadable: {exc})"]
    return [
        f"state.db:   {db_path} (schema v{version}, fts5={'yes' if fts else 'no'})",
        "counts:     " + ", ".join(f"{table}={count}" for table, count in counts.items()),
    ]


def _history_db_lines() -> list[str]:
    """Schema version and row counts for the keystone history.db (plan D2).

    Opened through ``paths.read_only_uri`` (percent-encoded ``mode=ro``) and
    named through ``history.storage.default_db_path`` — this function used to
    hand-build both, and the hand-built URI silently dropped ``mode=ro`` for a
    path containing ``#``, ``?`` or ``%``.
    """
    from . import paths
    from .history import storage as history_storage

    history_db = history_storage.default_db_path()
    if not history_db.exists():
        return [f"history.db: {history_db} (not created yet)"]
    try:
        with closing(sqlite3.connect(paths.read_only_uri(history_db), uri=True)) as conn:
            runs = conn.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0]
            cases = conn.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0]
            history_schema_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    except sqlite3.Error as exc:
        return [f"history.db: {history_db} (unreadable: {exc})"]
    return [f"history.db: {history_db} (schema v{history_schema_version}, "
            f"runs={runs}, cases={cases})"]


def _config_lines() -> list[str]:
    """Config location, the resolved summary, and credential PRESENCE booleans.

    Never the token values themselves.
    """
    import json as _json
    import os

    from . import config

    cfg_path = config.config_path()
    cfg = config.load_config()
    summary = {
        "detective": {k: cfg["detective"][k] for k in ("window_days", "min_fails",
                                                       "schedule")},
        "pipeline": cfg["pipeline"],
        "jira": {"base_url": cfg["jira"]["base_url"],
                 "enable_write": cfg["jira"]["enable_write"]},
        "incidents": cfg["incidents"],
    }
    return [
        f"config:     {cfg_path} ({'present' if cfg_path.exists() else 'defaults'})",
        "resolved config (summary):",
        _json.dumps(summary, indent=2, sort_keys=True),
        f"GITHUB_TOKEN set:   {bool(os.environ.get('GITHUB_TOKEN'))}",
        f"JIRA_API_TOKEN set: {bool(os.environ.get('JIRA_API_TOKEN'))}",
    ]


def status_text() -> str:
    """Human-readable state summary (plan Phase 7): DB paths, schema versions,
    per-table counts, config summary, credential presence — booleans only,
    never token values."""
    from . import paths

    return "\n".join([
        f"hermes-flaky-stabilization @ {paths.get_data_dir()}",
        *_state_db_lines(),
        *_history_db_lines(),
        *_config_lines(),
    ])
