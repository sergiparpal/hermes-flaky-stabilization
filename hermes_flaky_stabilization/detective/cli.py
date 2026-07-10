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
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import cronjobs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


# The argument definitions live in these adders, not inline, because the unified
# `flaky-stab` CLI mounts the same subcommands into its own flat subparser and
# used to transcribe every flag a second time (--window and --min-fails existed
# in four places). Each adder takes an already-created parser so the one-line
# `help=` stays at the call site, where it can name its namespace.


def add_detection_window_args(parser) -> None:
    """The two detection tunables, shared by ``scan`` and ``install-cron``."""
    parser.add_argument("--window", type=int, default=None,
                        help="Detection window in days (default: from config)")
    parser.add_argument("--min-fails", dest="min_fails", type=int, default=None,
                        help="Minimum failures in the window to be non-stable "
                             "(default: from config)")


def add_scan_args(parser) -> None:
    add_detection_window_args(parser)
    parser.add_argument("--include-errors", dest="include_errors",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Count `error` status as a failure (default: from config)")
    parser.add_argument("--format", choices=["human", "cron", "json"], default="human",
                        help="Output format (default: human)")


def add_list_args(parser) -> None:
    parser.add_argument("--status", choices=["flaky", "consistently_failing", "all"],
                        default="flaky", help="Which verdicts to list (default: flaky)")


def add_install_cron_args(parser) -> None:
    parser.add_argument("--schedule", default=None, help="Cron expression (default: from config)")
    parser.add_argument("--deliver", default=None, help="Delivery channel (default: from config)")
    add_detection_window_args(parser)
    parser.add_argument("--no-create", dest="no_create", action="store_true",
                        help="Only write the shim + config; print the command instead "
                             "of creating the job")


def setup_parser(subparser) -> None:
    """Define the detection subcommands (mounted by the unified ``flaky-stab`` CLI)."""
    subs = subparser.add_subparsers(dest="flaky_command", required=True)

    add_scan_args(subs.add_parser(
        "scan", help="Detect flaky tests, persist verdicts, and report"))

    subs.add_parser("status", help="Show resolved config, DB paths, and the last scan")

    add_list_args(subs.add_parser("list", help="List stored verdicts"))

    add_install_cron_args(subs.add_parser(
        "install-cron", help="Install the nightly no-agent detection job"))


def command_handlers() -> dict[str, object]:
    """Subcommand -> handler. The single dispatch table; the unified CLI uses a
    subset of it rather than re-deriving the mapping from private attr names."""
    return {
        "scan": _cmd_scan,
        "status": _cmd_status,
        "list": _cmd_list,
        "install-cron": _cmd_install_cron,
    }


def handle(args) -> int:
    """Dispatch to the selected subcommand. Returns a process exit code."""
    sub = getattr(args, "flaky_command", None)
    fn = command_handlers().get(sub)
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


def _scan_job() -> cronjobs.CronJob:
    """This module's job identity, read from the globals at CALL time (tests
    monkeypatch ``SHIM_NAME`` to simulate a missing shim source)."""
    return cronjobs.CronJob(SHIM_NAME, CRON_JOB_NAME, "cron job")


# Thin bindings over the shared installer: the mechanics (0700 dir + script, the
# `hermes cron create` subprocess and its print-the-command fallback) live in
# ``cronjobs``, which the unified CLI's jira-sync installer shares.
def _printable_cron_command(schedule: str, deliver: str) -> str:
    return cronjobs.printable_command(_scan_job(), schedule, deliver)


def _shim_source() -> Path:
    return cronjobs.shim_source(SHIM_NAME)


def _install_shim() -> Path:
    return cronjobs.install_shim(SHIM_NAME)


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
    # written into config.json nor baked into the scheduled job. The schedule is
    # validated too: it is passed as a positional argv to `hermes cron create`,
    # so a leading-dash value would be argument-injected as an option.
    try:
        domain.validate_tunables(window_days, min_fails)
        domain.validate_cron_schedule(schedule)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    job = _scan_job()

    # (0) verify the shim source BEFORE persisting anything: a missing source
    # must not leave config.json already mutated for a job that never installs.
    problem = cronjobs.missing_shim_error(job.shim_name)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1

    # (1) persist resolved options so the scheduled `scan` uses them.
    config.write_config({
        "schedule": schedule, "deliver": deliver,
        "window_days": window_days, "min_fails": min_fails,
    })

    # (2) install the shim.
    try:
        shim_dst = cronjobs.install_shim(job.shim_name)
    except OSError as exc:
        print(f"error: could not install the cron shim into <hermes_home>/scripts/: {exc}",
              file=sys.stderr)
        return 1
    print(f"installed shim: {shim_dst} (mode 0700)")

    # honor --no-create: shim + config written; just print the command.
    if args.no_create:
        print(f"skipping {job.description} creation (--no-create). To create it later, run:")
        cronjobs.print_manual_command(cronjobs.printable_command(job, schedule, deliver))
        return 0

    # (3) the one sanctioned subprocess.
    return cronjobs.create_job(job, schedule, deliver)
