"""Detection CLI subcommands, re-homed under ``hermes flaky-stab <subcommand>``.

Registered as a single top-level command via ``ctx.register_cli_command`` (see
``__init__.register``). ``setup_parser`` defines the argparse subcommands and
``handle`` dispatches them, returning a process exit code.

Subcommands: ``scan`` (detect + persist + report), ``status`` (resolved config +
paths + last scan), ``list`` (the stored verdicts), and ``install-cron`` (Phase 7
— wire the nightly no-agent job).
"""

import argparse
import json
import logging
import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def setup_parser(subparser) -> None:
    """Define the detection subcommands (mounted by the unified ``flaky-stab`` CLI)."""
    subs = subparser.add_subparsers(dest="flaky_command", required=True)

    p_scan = subs.add_parser("scan", help="Detect flaky tests, persist verdicts, and report")
    p_scan.add_argument("--window", type=int, default=None,
                        help="Detection window in days (default: from config)")
    p_scan.add_argument("--min-fails", dest="min_fails", type=int, default=None,
                        help="Minimum failures in the window to be non-stable (default: from config)")
    p_scan.add_argument("--include-errors", dest="include_errors",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Count `error` status as a failure (default: from config)")
    p_scan.add_argument("--format", choices=["human", "cron", "json"], default="human",
                        help="Output format (default: human)")

    subs.add_parser("status", help="Show resolved config, DB paths, and the last scan")

    p_list = subs.add_parser("list", help="List stored verdicts")
    p_list.add_argument("--status", choices=["flaky", "consistently_failing", "all"],
                        default="flaky", help="Which verdicts to list (default: flaky)")

    p_cron = subs.add_parser("install-cron", help="Install the nightly no-agent detection job")
    p_cron.add_argument("--schedule", default=None, help="Cron expression (default: from config)")
    p_cron.add_argument("--deliver", default=None, help="Delivery channel (default: from config)")
    p_cron.add_argument("--window", type=int, default=None, help="Detection window in days")
    p_cron.add_argument("--min-fails", dest="min_fails", type=int, default=None,
                        help="Minimum failures in the window to be non-stable")
    p_cron.add_argument("--no-create", dest="no_create", action="store_true",
                        help="Only write the shim + config; print the command instead of creating the job")


def handle(args) -> int:
    """Dispatch to the selected subcommand. Returns a process exit code."""
    handlers = {
        "scan": _cmd_scan,
        "status": _cmd_status,
        "list": _cmd_list,
        "install-cron": _cmd_install_cron,
    }
    sub = getattr(args, "flaky_command", None)
    fn = handlers.get(sub)
    if fn is None:
        print("error: no subcommand given (try `hermes flaky-stab --help`)")
        return 2
    return fn(args)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _arg_or_cfg(arg_value, cfg, key, cast):
    """A CLI override when given (not ``None``), else ``cfg[key]`` coerced via ``cast``."""
    return cast(cfg[key]) if arg_value is None else arg_value


def _resolve_scan_params(args, cfg):
    return (
        _arg_or_cfg(args.window, cfg, "window_days", int),
        _arg_or_cfg(args.min_fails, cfg, "min_fails", int),
        _arg_or_cfg(args.include_errors, cfg, "include_errors", bool),
    )


def _emit_scan(outcome, fmt, report_scope) -> int:
    """Render a :class:`scan.ScanOutcome` in ``fmt``; return the process exit code.

    All presentation lives here (the use-case layer returns data, never prints).
    Per-format rules: ``cron`` stays silent on stdout when test-history is
    unavailable (an empty tick, so the nightly job does not alert before results
    are ingested) and on a quiet/empty window; interactive formats surface the
    unavailable message with a non-zero exit.
    """
    from . import domain, reporting

    if outcome.kind == "unavailable":
        if fmt == "cron":
            print(outcome.message, file=sys.stderr)   # note to logs, silent stdout
            return 0
        print(f"error: {outcome.message}", file=sys.stderr)
        return 1

    if outcome.kind == "no_runs":
        if fmt == "json":
            print(reporting.render_json([], outcome.meta, [], []))
        elif fmt != "cron":  # human; cron stays silent (an empty tick)
            print(f"No test runs in the last {outcome.meta['window_days']} day(s); "
                  f"keeping the previous verdicts ({outcome.on_record} on record).")
        return 0

    # kind == "ok"
    if fmt == "json":
        print(reporting.render_json(outcome.verdicts, outcome.meta,
                                    outcome.newly_flaky, outcome.newly_resolved))
    elif fmt == "cron":
        flaky = [v for v in outcome.verdicts if v.status == domain.VERDICT_FLAKY]
        text = reporting.render_cron(
            report_scope, new_flaky_verdicts=flaky,
            newly_flaky=outcome.newly_flaky, newly_resolved=outcome.newly_resolved,
        )
        if text:
            print(text)
    else:  # human
        print(reporting.render_human(outcome.verdicts, outcome.meta))
    return 0


def _cmd_scan(args) -> int:
    from . import config, domain, scan, storage

    cfg = config.get_config()
    window_days, min_fails, include_errors = _resolve_scan_params(args, cfg)
    report_scope = cfg.get("report_scope", domain.DEFAULT_REPORT_SCOPE)
    fmt = args.format
    now = datetime.now(UTC)
    db_path = config.resolve_test_history_db_path(cfg)

    # The scan workflow (validate → read → detect → persist) lives in the use-case
    # layer; the CLI only resolves params, owns the verdicts connection, and renders.
    with storage.Storage() as store:
        try:
            outcome = scan.run_scan(
                store, window_days=window_days, min_fails=min_fails,
                include_errors=include_errors, db_path=db_path, now=now,
            )
        except ValueError as exc:   # invalid tunables — rejected before any read
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:    # any other failure (e.g. a SQLite error): emit a
            # clean one-line error and a non-zero exit rather than dumping a traceback.
            # The cron shim turns a non-zero exit into an alert, so the sweep still
            # fails loudly — just without the noise. Detail is logged server-side.
            logger.exception("flaky-stab scan failed")
            print(f"error: flaky-stab scan failed: {exc}", file=sys.stderr)
            return 1
        return _emit_scan(outcome, fmt, report_scope)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _cmd_status(args) -> int:
    from . import config, storage

    cfg = config.get_config()
    with storage.Storage() as store:
        last = store.last_scan_run()
        counts = store.count_by_status()

    print(f"verdicts_db:     {storage.get_db_path()}")
    print(f"config_path:     {config.config_path()}")
    print(f"test_history_db: {config.resolve_test_history_db_path(cfg)}")
    print("resolved config:")
    print(json.dumps(cfg, indent=2))
    if last:
        print(
            f"last scan:       {last['ran_at']} (window {last['window_days']}d, "
            f"min-fails {last['min_fails']}, include_errors={bool(last['include_errors'])}, "
            f"examined {last['tests_examined']}, flaky {last['flaky_found']}, "
            f"source_schema_version={last['source_schema_version']})"
        )
    else:
        print("last scan:       (never run — try `hermes flaky-stab scan`)")
    print(f"counts:          {counts if counts else '{}'}")
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(args) -> int:
    from . import domain, storage

    statuses = {
        "all": None,
        "flaky": [domain.VERDICT_FLAKY],
        "consistently_failing": [domain.VERDICT_CONSISTENTLY_FAILING],
    }[args.status]

    with storage.Storage() as store:
        rows = store.read_verdicts(statuses)
    if not rows:
        print("(no matching verdicts — run `hermes flaky-stab scan` first)")
        return 0
    for r in rows:
        print(
            f"{r['status']:>21}  {r['test_key']}  "
            f"[{r['fails']} fail / {r['passes']} pass of {r['runs']}]  "
            f"last_failure={r['last_failure'] or '-'}"
        )
    return 0


# ---------------------------------------------------------------------------
# install-cron (Phase 7)
# ---------------------------------------------------------------------------


SHIM_NAME = "flaky-stab-scan.sh"
CRON_JOB_NAME = "flaky-stabilization"


def _printable_cron_command(schedule: str, deliver: str) -> str:
    # shlex.quote the user-supplied parts so the printed copy-paste command stays a
    # single valid shell command even if a schedule or delivery channel contains
    # spaces or other shell metacharacters. (The real job creation uses argv, so it
    # was never injectable; this only hardens the human-facing fallback string.)
    return (f'hermes cron create {shlex.quote(schedule)} --no-agent --script {SHIM_NAME} '
            f'--deliver {shlex.quote(deliver)} --name {CRON_JOB_NAME}')


def _gateway_note() -> str:
    return ("Note: the gateway daemon must be running for the job to fire "
            "(`hermes gateway install`, then `hermes gateway`).")


def _print_manual_command(printable: str) -> None:
    """Print the manual ``hermes cron create`` command plus the gateway note."""
    print(f"  {printable}")
    print(_gateway_note())


def _shim_source() -> Path:
    """Path to the shim script shipped in the plugin checkout's ``scripts/`` dir."""
    return Path(__file__).resolve().parents[2] / "scripts" / SHIM_NAME


def _install_shim() -> Path:
    """Copy the shim into ``<hermes_home>/scripts/`` with mode 0700; return its path."""
    import shutil

    from . import storage

    scripts_dir = storage.get_hermes_home() / "scripts"   # scripts must live here (§1.3)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(scripts_dir, 0o700)
    except OSError:
        pass
    shim_src = _shim_source()
    shim_dst = scripts_dir / SHIM_NAME
    shutil.copyfile(shim_src, shim_dst)
    try:
        os.chmod(shim_dst, 0o700)
    except OSError:
        pass
    return shim_dst


def _cmd_install_cron(args) -> int:
    """Install the nightly no-agent cron job (the only sanctioned subprocess).

    Steps, in execution order: (0) verify the shim source exists, so a broken
    install (e.g. site-packages without the checkout's ``scripts/``) fails
    cleanly *before* anything is persisted; (1) persist the resolved options
    into ``config.json`` so the shim's ``scan`` uses them; (2) copy the shim
    into ``~/.hermes/scripts/`` (0700); (3) create the job once via
    ``hermes cron create``. If that CLI is missing or the gateway is not
    configured, this does **not** error — it prints the exact command plus a
    one-line gateway note. ``--no-create`` stops after step (2).
    """
    from . import config, domain

    cfg = config.get_config()
    schedule = args.schedule or cfg.get("schedule", domain.DEFAULT_SCHEDULE)
    deliver = args.deliver or cfg.get("deliver", domain.DEFAULT_DELIVER)
    window_days = _arg_or_cfg(args.window, cfg, "window_days", int)
    min_fails = _arg_or_cfg(args.min_fails, cfg, "min_fails", int)

    # Validate before persisting/installing anything, so a bad value is never
    # written into config.json nor baked into the scheduled job.
    try:
        domain.validate_tunables(window_days, min_fails)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # (0) verify the shim source BEFORE persisting anything: a missing source
    # must not leave config.json already mutated for a job that never installs.
    shim_src = _shim_source()
    if not shim_src.exists():
        print(f"error: cron shim source not found at {shim_src}; reinstall the "
              f"plugin from a checkout that ships scripts/{SHIM_NAME}, or copy that "
              "script into <hermes_home>/scripts/ yourself.", file=sys.stderr)
        return 1

    # (1) persist resolved options so the scheduled `scan` uses them.
    config.write_config({
        "schedule": schedule, "deliver": deliver,
        "window_days": window_days, "min_fails": min_fails,
    })

    # (2) install the shim.
    try:
        shim_dst = _install_shim()
    except OSError as exc:
        print(f"error: could not install the cron shim into <hermes_home>/scripts/: {exc}",
              file=sys.stderr)
        return 1
    print(f"installed shim: {shim_dst} (mode 0700)")

    printable = _printable_cron_command(schedule, deliver)

    # honor --no-create: shim + config written; just print the command.
    if args.no_create:
        print("skipping job creation (--no-create). To create it later, run:")
        _print_manual_command(printable)
        return 0

    # (3) the one sanctioned subprocess: create the job once. A cron-run session
    # cannot create cron jobs, so this is only ever reached from the standalone CLI.
    import subprocess

    cron_cmd = ["hermes", "cron", "create", schedule, "--no-agent",
                "--script", SHIM_NAME, "--deliver", deliver, "--name", CRON_JOB_NAME]
    try:
        result = subprocess.run(cron_cmd, capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        print(f"could not run the hermes CLI ({exc}). To create the job, run:")
        _print_manual_command(printable)
        return 0

    if result.returncode == 0:
        out = (result.stdout or "").strip()
        if out:
            print(out)
        print(f"created cron job '{CRON_JOB_NAME}' (verify with `hermes cron list`).")
        return 0

    # Non-zero (e.g. gateway not configured): fall back to printing the command.
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        print(detail)
    print("could not create the cron job automatically. To create it, run:")
    _print_manual_command(printable)
    return 0
