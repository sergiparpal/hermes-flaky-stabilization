"""Each CLI subcommand end-to-end against a temp HERMES_HOME."""

import argparse
import json
from pathlib import Path

from hermes_test_history import cli, storage

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _run(argv):
    parser = argparse.ArgumentParser(prog="test-history")
    cli.setup_parser(parser)
    return cli.handle(parser.parse_args(argv))


def test_status_empty(profile_env, capsys):
    assert _run(["status"]) == 0
    out = capsys.readouterr().out
    assert "db_path:" in out and "runs:" in out and "range:" in out


def test_config_prints_resolved_json(profile_env, capsys):
    assert _run(["config"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert cfg["default_lookback_days"] == 30
    assert cfg["max_stack_trace_chars"] == 500


def test_ingest_file_then_status(profile_env, capsys):
    assert _run(["ingest", str(FIXTURES / "pytest_junit_failures.xml")]) == 0
    capsys.readouterr()
    assert _run(["status"]) == 0
    conn = storage.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0] == 5


def test_ingest_directory(profile_env, capsys):
    assert _run(["ingest", str(FIXTURES)]) == 0
    assert "ingested 3 file(s)" in capsys.readouterr().out


def test_ingest_missing_path(profile_env, capsys):
    assert _run(["ingest", str(FIXTURES / "does_not_exist.xml")]) == 1
    assert "path not found" in capsys.readouterr().out


def test_prune_noop(profile_env, capsys):
    _run(["ingest", str(FIXTURES / "pytest_junit_failures.xml")])
    capsys.readouterr()
    assert _run(["prune", "--before", "1970-01-01"]) == 0
    assert "deleted 0 run(s)" in capsys.readouterr().out


def test_prune_bad_date_exit_1(profile_env, capsys):
    assert _run(["prune", "--before", "garbage"]) == 1
    assert "error" in capsys.readouterr().out


def test_prune_deletes_and_keeps_fts_consistent(profile_env):
    # basic=2026-05-01, jest=2026-05-10, failures=2026-05-18
    _run(["ingest", str(FIXTURES)])
    conn = storage.get_connection()
    assert _run(["prune", "--before", "2026-05-15"]) == 0
    cases = conn.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0]
    fts = conn.execute("SELECT COUNT(*) FROM test_cases_fts").fetchone()[0]
    assert fts == cases  # no FTS orphans after cascade prune
    # A test only present in a pruned run is gone from the FTS index too.
    gone = conn.execute(
        "SELECT COUNT(*) FROM test_cases_fts WHERE test_cases_fts MATCH 'test_add'"
    ).fetchone()[0]
    assert gone == 0


def test_rebuild_fts(profile_env, capsys):
    _run(["ingest", str(FIXTURES / "pytest_junit_failures.xml")])
    capsys.readouterr()
    assert _run(["rebuild-fts"]) == 0
    assert "rebuilt" in capsys.readouterr().out
    conn = storage.get_connection()
    hits = conn.execute(
        "SELECT COUNT(*) FROM test_cases_fts WHERE test_cases_fts MATCH 'oauth'"
    ).fetchone()[0]
    assert hits > 0
