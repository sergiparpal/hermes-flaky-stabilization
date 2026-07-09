"""CLI tests: register_cli wiring + status/config/sync handlers.

Unified-plugin adaptation: the subcommands mount under `flaky-stab jira` (no
set_defaults handler of their own, no hyphenated-name discovery alias), and the
default DB location is the consolidated state.db (plan D4/Appendix C)."""

import argparse
import json
import os

from jira_incidents import cli
from jira_incidents.store import IncidentStore


def _parser():
    p = argparse.ArgumentParser(prog="hermes flaky-stab jira")
    cli.register_cli(p)
    return p


def _run(ns):
    cli.hermes_jira_incidents_command(ns)


def test_register_cli_defines_subcommands():
    p = _parser()
    ns = p.parse_args(["status"])
    assert ns.action == "status"


def test_status_command_reports_stats(tmp_home, monkeypatch, capsys):
    # Seed a DB at the default (consolidated) location for this temp home.
    db = os.path.join(tmp_home, "flaky-stabilization", "state.db")
    s = IncidentStore(db)
    s.upsert({"key": "INC-1", "summary": "x", "status": "Open"})
    s.close()
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_home)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

    ns = _parser().parse_args(["status"])
    _run(ns)
    out = capsys.readouterr().out
    assert "incidents   : 1" in out
    assert "token set   : False" in out


def test_config_command_hides_secret(tmp_home, monkeypatch, capsys):
    cfg_dir = os.path.join(tmp_home, "flaky-stabilization")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump({"jira": {"base_url": "https://acme.atlassian.net",
                            "api_token": "LEAKED_SECRET"}}, f)
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_home)

    ns = _parser().parse_args(["config"])
    _run(ns)
    out = capsys.readouterr().out
    assert "acme.atlassian.net" in out
    assert "LEAKED_SECRET" not in out


def test_sync_command_without_creds_is_graceful(tmp_home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_home)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    ns = _parser().parse_args(["sync"])
    _run(ns)
    out = capsys.readouterr().out
    assert "Cannot sync" in out
