"""CLI tests: register_cli wiring + status/config/sync handlers.

Unified-plugin adaptation: the subcommands mount under `flaky-stab jira` (no
set_defaults handler of their own, no hyphenated-name discovery alias), and the
default DB location is the consolidated state.db (plan D4/Appendix C).

Exit-code contract (agreed with the top-level cli.py): the public entry point
`hermes_jira_incidents_command` returns an int exit code — 0 on success, 1 on
credential/config/Jira failure — which the top-level `run_cli` propagates so
the nightly cron shim can alert."""

import argparse
import json
import os

import pytest
from jira_incidents import cli
from jira_incidents import config as config_mod
from jira_incidents import jira_client as jc
from jira_incidents.store import IncidentStore


def _parser():
    p = argparse.ArgumentParser(prog="hermes flaky-stab jira")
    cli.register_cli(p)
    return p


def _run(ns) -> int:
    return cli.hermes_jira_incidents_command(ns)


def test_register_cli_defines_subcommands():
    p = _parser()
    ns = p.parse_args(["status"])
    assert ns.action == "status"


def test_sync_parser_accepts_full_and_quiet_flags():
    ns = _parser().parse_args(["sync", "--full", "--quiet"])
    assert ns.full is True
    assert ns.quiet is True
    ns = _parser().parse_args(["sync"])
    assert ns.full is False
    assert ns.quiet is False


def test_status_command_reports_stats(tmp_home, monkeypatch, capsys):
    # Seed a DB at the default (consolidated) location for this temp home.
    db = os.path.join(tmp_home, "flaky-stabilization", "state.db")
    s = IncidentStore(db)
    s.upsert({"key": "INC-1", "summary": "x", "status": "Open"})
    s.close()
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_home)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

    ns = _parser().parse_args(["status"])
    rc = _run(ns)
    out = capsys.readouterr().out
    assert rc == 0
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
    rc = _run(ns)
    out = capsys.readouterr().out
    assert rc == 0
    assert "acme.atlassian.net" in out
    assert "LEAKED_SECRET" not in out


def test_sync_command_without_creds_is_graceful_but_nonzero(tmp_home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_home)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    ns = _parser().parse_args(["sync"])
    rc = _run(ns)
    captured = capsys.readouterr()
    # A cron shim must be able to alert on this: non-zero exit, error on stderr.
    assert rc == 1
    assert "Cannot sync" in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# sync with a fake client: exit codes, --quiet, --full
# ---------------------------------------------------------------------------

def _write_cfg(home: str, jira: dict) -> None:
    cfg_dir = os.path.join(home, "flaky-stabilization")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump({"jira": jira}, f)


def _issue(key, updated="2024-01-05T00:00:00.000+0000"):
    return {"key": key, "fields": {"summary": f"issue {key}", "updated": updated}}


class _FakeClient:
    """Single page of canned issues; empty page for any token."""

    def __init__(self, issues):
        self._issues = issues

    def search(self, jql, fields=None, max_results=50, next_page_token=None):
        if next_page_token:
            return {"issues": []}
        return {"issues": self._issues}


class _PagingClient:
    """Serves `issues` one per page with a nextPageToken (offset-encoded)."""

    def __init__(self, issues):
        self._issues = issues

    def search(self, jql, fields=None, max_results=50, next_page_token=None):
        start = int(next_page_token) if next_page_token else 0
        resp = {"issues": self._issues[start:start + 1]}
        if start + 1 < len(self._issues):
            resp["nextPageToken"] = str(start + 1)
        return resp


class _ErrorClient:
    def search(self, *a, **k):
        raise jc.JiraError("Jira returned HTTP 401", status=401)


@pytest.fixture
def synced_env(tmp_home, monkeypatch):
    """A home with base_url configured and a token in the env."""
    _write_cfg(tmp_home, {"base_url": "https://x.atlassian.net"})
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_home)
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    return tmp_home


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(config_mod, "build_client", lambda cfg, token: client)


def test_sync_success_returns_zero(synced_env, monkeypatch, capsys):
    _patch_client(monkeypatch, _FakeClient([_issue("INC-1")]))
    rc = _run(_parser().parse_args(["sync"]))
    captured = capsys.readouterr()
    assert rc == 0
    assert "Sync complete" in captured.out
    assert captured.err == ""


def test_sync_jira_error_is_one_line_and_nonzero(synced_env, monkeypatch, capsys):
    _patch_client(monkeypatch, _ErrorClient())
    rc = _run(_parser().parse_args(["sync"]))
    captured = capsys.readouterr()
    assert rc == 1
    assert "Sync failed" in captured.err
    assert "Traceback" not in captured.err


def test_sync_quiet_suppresses_output_when_nothing_changed(synced_env, monkeypatch, capsys):
    _patch_client(monkeypatch, _FakeClient([_issue("INC-1")]))
    assert _run(_parser().parse_args(["sync"])) == 0
    capsys.readouterr()  # discard the first run's output
    # Steady state: the re-fetched issue is unchanged -> silent tick.
    rc = _run(_parser().parse_args(["sync", "--quiet"]))
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_sync_quiet_still_prints_when_something_changed(synced_env, monkeypatch, capsys):
    _patch_client(monkeypatch, _FakeClient([_issue("INC-1")]))
    rc = _run(_parser().parse_args(["sync", "--quiet"]))
    assert rc == 0
    assert "Sync complete" in capsys.readouterr().out


def test_sync_quiet_errors_still_go_to_stderr(tmp_home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_home)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    rc = _run(_parser().parse_args(["sync", "--quiet"]))
    captured = capsys.readouterr()
    assert rc == 1
    assert "Cannot sync" in captured.err


def test_full_sync_removes_locally_stale_incidents(synced_env, monkeypatch, capsys):
    """SCENARIO: an issue deleted/re-keyed in Jira lingers in the local index
    forever (retention_days defaults to 0). `sync --full` re-ingests everything
    and expunges local keys the completed run did not see."""
    db = os.path.join(synced_env, "flaky-stabilization", "state.db")
    s = IncidentStore(db)
    s.upsert({"key": "INC-GONE", "summary": "expunged upstream", "status": "Open"})
    s.set_watermark("2024-01-01T00:00:00Z")
    s.set_meta("backfill_done", "1")
    s.close()
    _patch_client(monkeypatch, _FakeClient([_issue("INC-LIVE")]))

    rc = _run(_parser().parse_args(["sync", "--full"]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Removed 1" in out
    s = IncidentStore(db)
    try:
        assert s.get("INC-GONE") is None
        assert s.get("INC-LIVE") is not None
    finally:
        s.close()


def test_full_sync_truncated_run_skips_deletion(synced_env, monkeypatch, capsys):
    # max_pages=1 with two one-issue pages -> the full run is truncated, so
    # the not-seen-key deletion must be aborted (INC-GONE survives).
    _write_cfg(synced_env, {"base_url": "https://x.atlassian.net",
                            "page_size": 1, "max_pages": 1})
    db = os.path.join(synced_env, "flaky-stabilization", "state.db")
    s = IncidentStore(db)
    s.upsert({"key": "INC-GONE", "summary": "maybe expunged", "status": "Open"})
    s.close()
    _patch_client(monkeypatch, _PagingClient([_issue("INC-A"), _issue("INC-B")]))

    rc = _run(_parser().parse_args(["sync", "--full"]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipping deletion" in out
    s = IncidentStore(db)
    try:
        assert s.get("INC-GONE") is not None  # deletion aborted
        assert s.get("INC-A") is not None     # but the page seen was ingested
    finally:
        s.close()
