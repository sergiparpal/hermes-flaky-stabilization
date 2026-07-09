"""Sanity tests for the Phase-0 test doubles (plan step 0.3)."""

import pytest
from conftest import VALID_HOOKS, FakeTransport


def _noop_handler(params, **kwargs):
    return "{}"


def test_fake_ctx_records_tool_registration(fake_ctx):
    fake_ctx.register_tool(
        "demo_tool",
        toolset="flaky_healer",
        schema={"name": "demo_tool", "description": "d", "parameters": {}},
        handler=_noop_handler,
        check_fn=lambda: True,
    )
    assert "demo_tool" in fake_ctx.tools
    entry = fake_ctx.tools["demo_tool"]
    assert entry["toolset"] == "flaky_healer"
    assert entry["check_fn"]() is True


def test_fake_ctx_validates_hook_names(fake_ctx):
    fake_ctx.register_hook("pre_approval_request", lambda *a, **k: None)
    assert len(fake_ctx.hooks["pre_approval_request"]) == 1
    with pytest.raises(AssertionError):
        fake_ctx.register_hook("not_a_real_hook", lambda *a, **k: None)
    assert "pre_approval_request" in VALID_HOOKS and len(VALID_HOOKS) == 15


def test_fake_ctx_fire_hook_invokes_callbacks(fake_ctx):
    seen = []
    fake_ctx.register_hook("post_approval_response", lambda payload, **kw: seen.append(payload))
    fake_ctx.fire_hook("post_approval_response", {"approved": True})
    assert seen == [{"approved": True}]


def test_dispatch_tool_records_and_returns_canned(fake_ctx):
    fake_ctx.dispatch_results["terminal"] = {"stdout": "", "exit_code": 0}
    out = fake_ctx.dispatch_tool("terminal", {"command": "git status"})
    assert out == {"stdout": "", "exit_code": 0}
    assert fake_ctx.dispatched == [{"tool": "terminal", "arguments": {"command": "git status"}}]
    # unknown tools still succeed with a default
    assert fake_ctx.dispatch_tool("mystery", {}) == {"status": "ok"}


def test_dispatch_executor_takes_precedence(fake_ctx):
    fake_ctx.dispatch_executor = lambda name, args: {"ran": name}
    assert fake_ctx.dispatch_tool("terminal", {"command": "true"}) == {"ran": "terminal"}
    assert fake_ctx.dispatched[-1]["tool"] == "terminal"


def test_fake_llm_queue_and_exhaustion(fake_ctx):
    fake_ctx.llm.queue({"cause": "timeout"})
    out = fake_ctx.llm.complete_structured(instructions="i", input={"x": 1}, schema={})
    assert out == {"cause": "timeout"}
    assert len(fake_ctx.llm.structured_calls) == 1
    with pytest.raises(AssertionError):
        fake_ctx.llm.complete_structured(instructions="i", input={}, schema={})


def test_fake_transport_routes_and_records():
    t = FakeTransport()
    t.add("GET", "https://api.example/x", {"ok": True})
    status, headers, body = t.request("get", "https://api.example/x", headers={"A": "b"})
    assert status == 200 and b'"ok"' in body
    assert t.requests[0]["headers"] == {"A": "b"}
    status, _, _ = t.request("GET", "https://api.example/missing")
    assert status == 404
