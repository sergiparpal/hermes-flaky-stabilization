"""Phase-5 acceptance: incidents wiring through the *unified* register path.

The ported suite covers the service in depth; these tests pin what the
unification must guarantee — the pre_llm_call injection contract, the
session-start sync trigger, check_fn offline semantics, the `flaky-stab jira
sync` CLI against a fake client, and the kind-coercion string guard.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from _doubles import FakePluginContext

PACKAGE = Path(__file__).resolve().parent.parent / "hermes_flaky_stabilization"


def _register(monkeypatch=None):
    import hermes_flaky_stabilization as plugin

    ctx = FakePluginContext()
    plugin.register(ctx)
    return ctx


def _seed_store(home: Path) -> None:
    from hermes_flaky_stabilization.incidents.store import IncidentStore

    store = IncidentStore(str(home / "flaky-stabilization" / "state.db"))
    store.upsert({
        "key": "INC-42",
        "summary": "checkout outage, contact ops@corp.com",
        "status": "Resolved",
        "root_cause": "pool exhaustion",
        "body": "details",
    })
    store.close()
    return None


# --- pre_llm_call injection ------------------------------------------------------


def test_pre_llm_call_returns_redacted_context(_isolated_env):
    _seed_store(_isolated_env)
    ctx = _register()
    results = ctx.fire_hook("pre_llm_call", user_message="checkout outage",
                            session_id="s1", is_first_turn=True)
    payloads = [r for r in results if r]
    assert len(payloads) == 1
    context = payloads[0]["context"]
    assert "INC-42" in context
    assert "[redacted-email]" in context
    assert "ops@corp.com" not in context


def test_pre_llm_call_none_when_config_flag_off(_isolated_env):
    _seed_store(_isolated_env)
    from hermes_flaky_stabilization import config as unified_config

    unified_config.write_config({"incidents": {"context_injection": False}})
    ctx = _register()
    results = ctx.fire_hook("pre_llm_call", user_message="checkout outage")
    assert all(r is None for r in results)


def test_pre_llm_call_bounded_by_timeout_with_slow_store(_isolated_env):
    _seed_store(_isolated_env)
    from hermes_flaky_stabilization import config as unified_config

    unified_config.write_config({"incidents": {"prefetch_timeout": 0.2}})

    class SlowStore:
        def search(self, query, limit=3):
            time.sleep(5.0)
            return []

        def count(self):
            return 1

        def close(self):
            pass

    ctx = _register()
    # Reach the service via its bound check_fn: initialize it, then swap in
    # the deliberately slow store.
    ctx.fire_hook("on_session_start", session_id="s1")
    service = _service_via_checkfn(ctx)
    service._store = SlowStore()
    start = time.time()
    results = ctx.fire_hook("pre_llm_call", user_message="anything unique zz")
    elapsed = time.time() - start
    assert all(r is None for r in results)  # degraded to no-injection
    assert elapsed < 2.0


def _service_via_checkfn(ctx):
    check = ctx.tools["jira_search_incident"]["check_fn"]
    return check.__self__


# --- on_session_start ------------------------------------------------------------


def test_on_session_start_silent_without_credentials(_isolated_env):
    ctx = _register()
    results = ctx.fire_hook("on_session_start", session_id="s1")
    assert all(r is None for r in results)
    service = _service_via_checkfn(ctx)
    assert service._client is None  # no creds -> read-only index, no sync thread


def test_on_session_start_triggers_background_sync_with_creds(_isolated_env, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "qa@example.com")
    ctx = _register()

    class FakeClient:
        calls = 0

        def search(self, jql, fields=None, max_results=50, next_page_token=None):
            FakeClient.calls += 1
            return {"issues": []}

    service = _service_via_checkfn(ctx)
    monkeypatch.setattr(
        "hermes_flaky_stabilization.incidents.config.build_client",
        lambda cfg, token: FakeClient(),
    )
    ctx.fire_hook("on_session_start", session_id="s1")
    service.join_sync(2.0)
    assert FakeClient.calls >= 1  # the background ingest actually ran


# --- check_fn offline semantics ----------------------------------------------------


def test_check_fn_true_offline_when_index_populated(_isolated_env, monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    _seed_store(_isolated_env)
    ctx = _register()
    assert ctx.tools["jira_search_incident"]["check_fn"]()


def test_check_fn_false_without_creds_and_empty_index(_isolated_env, monkeypatch):
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    ctx = _register()
    assert not ctx.tools["jira_get_root_cause"]["check_fn"]()


# --- flaky-stab jira sync (fake client) --------------------------------------------


def _issue(key: str, updated: str):
    return {
        "key": key,
        "fields": {
            "summary": f"incident {key}",
            "status": {"name": "Open"},
            "created": "2026-01-01T00:00:00.000+0000",
            "updated": updated,
            "description": None,
        },
    }


def test_cli_jira_sync_populates_index_and_is_incrementally_quiet(
        _isolated_env, monkeypatch, capsys):
    from hermes_flaky_stabilization import cli
    from hermes_flaky_stabilization.incidents import cli as incidents_cli
    from hermes_flaky_stabilization.incidents import config as config_mod
    from hermes_flaky_stabilization.incidents.store import IncidentStore

    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    monkeypatch.setattr(incidents_cli, "_hermes_home", lambda: str(_isolated_env))

    class FakeClient:
        def search(self, jql, fields=None, max_results=50, next_page_token=None):
            if next_page_token:
                return {"issues": []}
            return {"issues": [_issue("INC-1", "2026-07-01T10:00:00.000+0000"),
                               _issue("INC-2", "2026-07-02T10:00:00.000+0000")]}

    monkeypatch.setattr(config_mod, "build_client", lambda cfg, token: FakeClient())

    parser = argparse.ArgumentParser(prog="flaky-stab")
    cli.setup_cli(parser)

    assert cli.run_cli(parser.parse_args(["jira", "sync"])) == 0
    first = capsys.readouterr().out
    assert "ingested 2" in first

    store = IncidentStore(str(_isolated_env / "flaky-stabilization" / "state.db"))
    try:
        assert store.count() == 2
        assert store.get_watermark() == "2026-07-02T10:00:00.000+0000"
    finally:
        store.close()

    # Second run with identical upstream data: zero changed rows (no FTS churn).
    assert cli.run_cli(parser.parse_args(["jira", "sync"])) == 0
    second = capsys.readouterr().out
    assert "ingested 0" in second

    assert cli.run_cli(parser.parse_args(["jira", "status"])) == 0
    status = capsys.readouterr().out
    assert "incidents   : 2" in status


# --- kind auto-detection guard across the whole package ----------------------------


def test_memory_marker_strings_absent_from_entire_package():
    """Plan §3.2 / Phase 5: the coercion marker strings must appear nowhere in
    the package — not just the entry files (they could migrate on refactor)."""
    markers = ("register_memory" + "_provider", "Memory" + "Provider")
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in src, f"{path.relative_to(PACKAGE)} contains {marker!r}"


def test_llm_context_survives_uninitialized_service(_isolated_env):
    """The injection path must never raise, even on a fresh, empty profile."""
    ctx = _register()
    results = ctx.fire_hook("pre_llm_call", user_message="anything")
    assert all(r is None for r in results)
