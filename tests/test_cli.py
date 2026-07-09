"""End-to-end unified CLI: ingest a JUnit fixture, scan, see the flaky verdict.

Exercises the real argparse wiring (``setup_cli`` + ``run_cli``) the way the
host would after ``register_cli_command``, inside a throwaway HERMES_HOME.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

import pytest


def _make_parser():
    from hermes_flaky_stabilization import cli

    parser = argparse.ArgumentParser(prog="flaky-stab")
    cli.setup_cli(parser)
    return parser, cli


def _run(argv: list[str], capsys) -> tuple[int, str]:
    parser, cli = _make_parser()
    rc = cli.run_cli(parser.parse_args(argv))
    return rc, capsys.readouterr().out


def _junit_xml(name_status_pairs, ts: datetime) -> str:
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%S")
    cases = []
    for name, status in name_status_pairs:
        body = '<failure message="boom" type="AssertionError">trace</failure>' \
            if status == "failed" else ""
        cases.append(
            f'<testcase classname="shop.cart" name="{name}" file="src/shop/test_cart.py" '
            f'time="0.01">{body}</testcase>'
        )
    failures = sum(1 for _, s in name_status_pairs if s == "failed")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuite name="cli-suite" timestamp="{stamp}" tests="{len(cases)}" '
        f'failures="{failures}" errors="0" skipped="0">{"".join(cases)}</testsuite>'
    )


@pytest.fixture
def history_reset():
    from hermes_flaky_stabilization.history import storage

    storage.reset_for_tests()
    yield
    storage.reset_for_tests()


def test_ingest_then_scan_reports_the_flaky_test(profile_env, history_reset,
                                                 tmp_path, capsys):
    now = datetime.now(UTC).replace(tzinfo=None)
    day = timedelta(days=1)
    schedule = [
        (now - 6 * day, "failed"),
        (now - 4 * day, "failed"),
        (now - 3 * day, "passed"),
        (now - 2 * day, "failed"),
    ]
    for i, (ts, status) in enumerate(schedule):
        xml = tmp_path / f"run{i}.xml"
        xml.write_text(_junit_xml([("kw_checkout", status)], ts), encoding="utf-8")
        rc, out = _run(["ingest", str(xml)], capsys)
        assert rc == 0, out
        assert "ingested" in out

    rc, out = _run(["scan", "--format", "json"], capsys)
    assert rc == 0, out
    payload = json.loads(out)
    verdicts = {v["test_key"]: v for v in payload["verdicts"]}
    assert verdicts["shop.cart::kw_checkout"]["status"] == "flaky"

    rc, out = _run(["list", "--status", "flaky"], capsys)
    assert rc == 0
    assert "shop.cart::kw_checkout" in out


def test_status_smoke(profile_env, capsys):
    rc, out = _run(["status"], capsys)
    assert rc == 0
    assert "state.db" in out


def test_test_history_alias_exposes_history_cli(profile_env, history_reset, capsys):
    """The kept `test-history` CLI contract (plan D2): same subcommands, same
    behavior, registered as its own top-level command."""
    from _doubles import FakePluginContext

    import hermes_flaky_stabilization as plugin

    ctx = FakePluginContext()
    plugin.register(ctx)
    alias = ctx.cli_commands["test-history"]
    parser = argparse.ArgumentParser(prog="test-history")
    alias["setup_fn"](parser)
    rc = alias["handler_fn"](parser.parse_args(["status"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "db_path" in out and "history.db" in out
