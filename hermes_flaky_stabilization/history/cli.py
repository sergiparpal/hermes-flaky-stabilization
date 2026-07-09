"""CLI subcommands for ``hermes test-history <subcommand>``.

Registered as a single top-level command via ``ctx.register_cli_command`` (see
``__init__.register``). ``setup_parser`` defines the argparse subcommands and
``handle`` dispatches them, returning a process exit code.
"""

import json
import os
from pathlib import Path

from . import domain
from .storage import get_config, get_connection, get_db_path

# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def setup_parser(subparser) -> None:
    """Define ``test-history`` subcommands. Called by Hermes during registration."""
    subs = subparser.add_subparsers(dest="test_history_command", required=True)

    p_ingest = subs.add_parser("ingest", help="Ingest a JUnit XML file or a directory of them (recursive)")
    p_ingest.add_argument("path", type=str, help="Path to an .xml file or a directory")

    subs.add_parser("status", help="Show DB path, counts, and date range")

    p_prune = subs.add_parser("prune", help="Delete runs older than --before (cascades to cases)")
    p_prune.add_argument("--before", required=True, type=str, help="ISO-8601 date/time cutoff")

    subs.add_parser("rebuild-fts", help="Rebuild the FTS5 index from the base table")

    subs.add_parser("config", help="Show the resolved configuration")


def handle(args) -> int:
    """Dispatch to the selected subcommand. Returns a process exit code."""
    handlers = {
        "ingest": _cmd_ingest,
        "status": _cmd_status,
        "prune": _cmd_prune,
        "rebuild-fts": _cmd_rebuild_fts,
        "config": _cmd_config,
    }
    sub = getattr(args, "test_history_command", None)
    fn = handlers.get(sub)
    if fn is None:
        print("error: no subcommand given (try `hermes test-history --help`)")
        return 2
    return fn(args)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _cmd_ingest(args) -> int:
    from .ingest import ingest_directory, ingest_file  # lazy (hard-constraint #2)

    target = Path(os.path.realpath(args.path))         # hard-constraint #10
    if not target.exists():
        print(f"error: path not found: {target}")
        return 1
    conn = get_connection()
    if target.is_dir():
        ids, skipped = ingest_directory(conn, target)
        suffix = f" ({skipped} skipped)" if skipped else ""
        print(f"ingested {len(ids)} file(s) from {target}{suffix}")
    else:
        try:
            run_id = ingest_file(conn, target)
        except Exception as exc:  # noqa: BLE001 — friendly CLI error, no traceback
            print(f"error: could not ingest {target}: {exc}")
            return 1
        print(f"ingested run_id={run_id} from {target}")
    return 0


def _cmd_status(args) -> int:
    conn = get_connection()
    runs = conn.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0]
    cases = conn.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0]
    try:
        fts_rows = conn.execute("SELECT COUNT(*) FROM test_cases_fts").fetchone()[0]
    except Exception:
        fts_rows = "n/a"
    # `et` is a static SQL fragment (the shared effective-time rule from domain),
    # not a value, so concatenating it is injection-safe; real values still bind.
    et = domain.effective_time()
    row = conn.execute(
        "SELECT MIN(datetime(" + et + ")), MAX(datetime(" + et + ")) FROM test_runs"
    ).fetchone()
    earliest, latest = row[0], row[1]
    print(f"db_path:  {get_db_path()}")
    print(f"runs:     {runs}")
    print(f"cases:    {cases}")
    print(f"fts_rows: {fts_rows}")
    print(f"range:    {earliest or '-'} .. {latest or '-'}")
    return 0


def _cmd_prune(args) -> int:
    from .timeutil import parse_iso_window  # shared ISO-8601 parser/validator

    try:
        cutoff = parse_iso_window(args.before, "--before")
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    conn = get_connection()
    # Delete child cases first: a direct DELETE always fires the FTS5
    # delete-trigger, so the index stays consistent regardless of how FK-cascade
    # interacts with recursive_triggers. Then delete the now-orphaned runs.
    # `et` is a static SQL fragment (domain's effective-time rule); cutoff binds via ?.
    et = domain.effective_time()
    conn.execute(
        "DELETE FROM test_cases WHERE run_id IN ("
        "  SELECT id FROM test_runs "
        "  WHERE datetime(" + et + ") < datetime(?))",
        (cutoff,),
    )
    cur = conn.execute(
        "DELETE FROM test_runs WHERE datetime(" + et + ") < datetime(?)",
        (cutoff,),
    )
    conn.commit()
    print(f"deleted {cur.rowcount} run(s) older than {cutoff}")
    return 0


def _cmd_rebuild_fts(args) -> int:
    conn = get_connection()
    conn.execute("INSERT INTO test_cases_fts(test_cases_fts) VALUES('rebuild')")
    conn.commit()
    print("FTS5 index rebuilt")
    return 0


def _cmd_config(args) -> int:
    print(json.dumps(get_config(), indent=2))
    return 0
