"""End-to-end CLI tests (``scan`` / ``status`` / ``list``) on a temp HERMES_HOME.

The synthetic test-history DB is placed at the *default* resolved location
(``<home>/test-history/history.db``) so the whole resolution path is exercised.
Run timestamps are relative to the real clock so the rows always fall inside the
detection window regardless of when the suite runs.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta

from fixtures import build_test_history_db
from hermes_flaky_detective import cli, domain, storage

_NOW = datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def _days_ago(n: int) -> str:
    return (_NOW - timedelta(days=n)).isoformat()


def _run(argv):
    parser = argparse.ArgumentParser(prog="flaky-detective")
    cli.setup_parser(parser)
    return cli.handle(parser.parse_args(argv))


def _seed_test_history(home, *, flaky_statuses=("failed", "failed", "failed", "passed")):
    """Create <home>/test-history/history.db with a flaky test and a stable test."""
    th_dir = home / "test-history"
    th_dir.mkdir(parents=True, exist_ok=True)
    runs = []
    for i, status in enumerate(flaky_statuses):
        runs.append({
            "source_file": f"ci/run-{i}.xml",
            "run_timestamp": _days_ago(len(flaky_statuses) - i),
            "cases": [
                {"classname": "pkg.Mod", "name": "test_flaky", "file_path": "src/mod.py",
                 "status": status},
                {"classname": "pkg.Mod", "name": "test_stable", "file_path": "src/mod.py",
                 "status": "passed"},
            ],
        })
    return build_test_history_db(th_dir / "history.db", runs)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_runs_against_real_install(profile_env, capsys):
    assert _run(["status"]) == 0
    out = capsys.readouterr().out
    assert "verdicts_db:" in out
    assert "test_history_db:" in out
    assert "resolved config:" in out
    assert '"window_days": 14' in out
    assert "never run" in out          # no scan yet


# ---------------------------------------------------------------------------
# scan — end-to-end detection
# ---------------------------------------------------------------------------


def test_scan_detects_flaky_end_to_end(profile_env, capsys):
    _seed_test_history(profile_env)
    assert _run(["scan", "--format", "human"]) == 0
    out = capsys.readouterr().out
    assert "test_flaky" in out
    assert "1 flaky" in out
    # persisted — re-open the verdicts DB the CLI wrote and closed
    with storage.Storage() as store:
        v = store.get_verdict("pkg.Mod::test_flaky")
        assert v is not None and v["status"] == domain.VERDICT_FLAKY
        assert store.get_verdict("pkg.Mod::test_stable")["status"] == domain.VERDICT_STABLE
        # a scan_runs row was recorded
        assert store.last_scan_run()["flaky_found"] == 1


def test_scan_json_format(profile_env, capsys):
    _seed_test_history(profile_env)
    assert _run(["scan", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scan"]["flaky_found"] == 1
    assert payload["changes"]["newly_flaky"] == ["pkg.Mod::test_flaky"]
    keys = {v["test_key"] for v in payload["verdicts"]}
    assert {"pkg.Mod::test_flaky", "pkg.Mod::test_stable"} <= keys


def test_scan_cron_prints_nothing_when_no_changes(profile_env, capsys):
    _seed_test_history(profile_env)
    # First cron scan: the test becomes newly flaky -> non-empty output.
    assert _run(["scan", "--format", "cron"]) == 0
    first = capsys.readouterr().out
    assert "now flaky" in first and "test_flaky" in first
    # Second cron scan, identical data: no change -> silent (empty stdout).
    assert _run(["scan", "--format", "cron"]) == 0
    assert capsys.readouterr().out == ""


def test_scan_cron_full_set_lists_current(profile_env, capsys, monkeypatch):
    from hermes_flaky_detective import config
    _seed_test_history(profile_env)
    # Force full-set scope via config.json.
    config.write_config({"report_scope": "full-set"})
    assert _run(["scan", "--format", "cron"]) == 0
    out = capsys.readouterr().out
    assert "Flaky tests currently detected" in out
    assert "test_flaky" in out


def test_scan_window_and_min_fails_overrides(profile_env, capsys):
    _seed_test_history(profile_env)
    # min-fails 4 with only 3 failures -> not flaky anymore (stable).
    assert _run(["scan", "--min-fails", "4", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scan"]["flaky_found"] == 0
    assert payload["scan"]["min_fails"] == 4


def test_scan_no_include_errors_flag(profile_env, capsys):
    # 2 failed + 1 error + 1 passed. With errors counted (default) -> 3 fails -> flaky.
    # With --no-include-errors -> 2 fails -> stable.
    _seed_test_history(profile_env, flaky_statuses=("failed", "failed", "error", "passed"))
    assert _run(["scan", "--no-include-errors", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scan"]["flaky_found"] == 0
    assert payload["scan"]["include_errors"] is False


def test_scan_include_errors_default_counts_error(profile_env, capsys):
    _seed_test_history(profile_env, flaky_statuses=("failed", "failed", "error", "passed"))
    assert _run(["scan", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scan"]["flaky_found"] == 1
    assert payload["scan"]["include_errors"] is True


# ---------------------------------------------------------------------------
# scan — tunable validation
# ---------------------------------------------------------------------------


def test_scan_rejects_nonpositive_min_fails(profile_env, capsys):
    # min_fails <= 0 would mark every all-passing test flaky -> reject (exit 2),
    # before even touching test-history.
    assert _run(["scan", "--min-fails", "0", "--format", "json"]) == 2
    assert "min-fails" in capsys.readouterr().err


def test_scan_rejects_nonpositive_window(profile_env, capsys):
    assert _run(["scan", "--window", "0"]) == 2
    assert "window" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# scan — empty window must not wipe the previous snapshot
# ---------------------------------------------------------------------------


def test_scan_empty_window_preserves_previous_verdicts(profile_env, capsys):
    # First, a normal scan records a flaky verdict.
    _seed_test_history(profile_env)
    assert _run(["scan", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["scan"]["flaky_found"] == 1

    # Re-seed the test-history DB with only ancient runs (older than the window),
    # so the next scan reads zero rows.
    th = profile_env / "test-history" / "history.db"
    th.unlink()
    old = (_NOW - timedelta(days=400)).isoformat()
    build_test_history_db(th, [{"source_file": "old.xml", "run_timestamp": old, "cases": [
        {"classname": "pkg.Mod", "name": "test_flaky", "file_path": "src/mod.py", "status": "passed"},
    ]}])

    assert _run(["scan", "--format", "human"]) == 0
    assert "keeping the previous verdicts" in capsys.readouterr().out
    # The earlier flaky verdict survives the empty scan (not overwritten).
    with storage.Storage() as store:
        assert store.get_verdict("pkg.Mod::test_flaky")["status"] == domain.VERDICT_FLAKY


# ---------------------------------------------------------------------------
# scan — test-history unavailable
# ---------------------------------------------------------------------------


def test_scan_human_errors_when_test_history_missing(profile_env, capsys):
    assert _run(["scan", "--format", "human"]) == 1
    err = capsys.readouterr().err
    assert "not found" in err


def test_scan_cron_silent_when_test_history_missing(profile_env, capsys):
    assert _run(["scan", "--format", "cron"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""               # silent tick on stdout
    assert "not found" in captured.err      # note still on stderr


def test_scan_internal_error_is_clean_not_a_traceback(profile_env, capsys, monkeypatch):
    # An unexpected failure inside the use case (here, a SQLite error) must surface as
    # a one-line error + non-zero exit, not an uncaught traceback. The cron shim turns
    # the non-zero exit into an alert, so the sweep still fails loudly.
    import sqlite3

    from hermes_flaky_detective import scan

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scan, "run_scan", boom)
    assert _run(["scan", "--format", "human"]) == 1
    err = capsys.readouterr().err
    assert "scan failed" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_flaky_after_scan(profile_env, capsys):
    _seed_test_history(profile_env)
    _run(["scan", "--format", "human"])
    capsys.readouterr()
    assert _run(["list", "--status", "flaky"]) == 0
    out = capsys.readouterr().out
    assert "pkg.Mod::test_flaky" in out
    assert "pkg.Mod::test_stable" not in out


def test_list_all_after_scan(profile_env, capsys):
    _seed_test_history(profile_env)
    _run(["scan", "--format", "human"])
    capsys.readouterr()
    assert _run(["list", "--status", "all"]) == 0
    out = capsys.readouterr().out
    assert "pkg.Mod::test_flaky" in out and "pkg.Mod::test_stable" in out


def test_list_empty_before_scan(profile_env, capsys):
    assert _run(["list", "--status", "flaky"]) == 0
    assert "no matching verdicts" in capsys.readouterr().out
