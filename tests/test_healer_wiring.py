"""Phase-4 acceptance: healer wiring through the *unified* register path.

The ported healer suite covers the stage in depth; these tests pin what the
unification itself must guarantee — check_fn gating, and the audit observer
hooks writing redacted rows into the consolidated state.db.
"""

from __future__ import annotations

import json
from contextlib import closing

from _doubles import FakePluginContext


def _register(tmp_path, monkeypatch):
    import hermes_flaky_stabilization as plugin

    monkeypatch.setenv("FLAKY_HEALER_DATA_DIR", str(tmp_path / "healer-data"))
    ctx = FakePluginContext(hermes_home=tmp_path / "hermes-home")
    plugin.register(ctx)
    return ctx


def test_fetch_ci_logs_hidden_without_github_token(tmp_path, monkeypatch):
    ctx = _register(tmp_path, monkeypatch)
    check = ctx.tools["fetch_ci_logs"]["check_fn"]
    assert callable(check)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert not check()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_dummy")
    assert check()


def test_other_healer_tools_have_no_check_fn(tmp_path, monkeypatch):
    ctx = _register(tmp_path, monkeypatch)
    for name in ("analyze_playwright_trace", "heal_flaky_test", "list_healing_recipes"):
        assert ctx.tools[name]["check_fn"] is None, name


def test_skill_registered_with_existing_path(tmp_path, monkeypatch):
    ctx = _register(tmp_path, monkeypatch)
    # FakePluginContext.register_skill already asserts the file exists.
    assert ctx.skills["flaky-healer"].endswith("skills/flaky-healer/SKILL.md")


def test_audit_hooks_write_redacted_rows_into_state_db(tmp_path, monkeypatch):
    ctx = _register(tmp_path, monkeypatch)

    ctx.fire_hook(
        "pre_approval_request",
        command="git push -u origin fix/flaky-x",
        description="push branch",
        pattern_key="git_push",
        session_key="s1",
        surface="cli",
    )
    ctx.fire_hook(
        "post_approval_response",
        command="git push -u origin fix/flaky-x",
        choice="once",
        pattern_key="git_push",
        session_key="s1",
        token="ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    )

    from hermes_flaky_stabilization.storage import state

    db_path = tmp_path / "healer-data" / "state.db"
    assert db_path.exists()
    with closing(state.connect(db_path)) as conn:
        rows = conn.execute("SELECT event, payload_json FROM audit ORDER BY id").fetchall()
    assert [r["event"] for r in rows] == ["pre_approval_request", "post_approval_response"]
    payloads = json.dumps([json.loads(r["payload_json"]) for r in rows])
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" not in payloads  # token masked
