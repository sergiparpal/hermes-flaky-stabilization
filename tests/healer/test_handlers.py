"""Handler contract tests: every tool returns a valid JSON string for
happy and error paths (plan §7 canonical file)."""

import json

import handlers
import pytest
from conftest import FIXTURES, FakeTransport

REPO = "acme/webshop"
RUN_ID = "9876543210"
BASE = "https://api.github.com"


@pytest.fixture
def gh_transport():
    t = FakeTransport()
    t.add(
        "GET",
        f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}",
        json.loads((FIXTURES / "gh" / "run.json").read_text()),
    )
    t.add(
        "GET",
        f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}/jobs?per_page=100&page=1",
        json.loads((FIXTURES / "gh" / "jobs.json").read_text()),
    )
    t.add(
        "GET",
        f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}/logs",
        (FIXTURES / "gh" / "run_logs.zip").read_bytes(),
    )
    return t


def _call(fn, params, **kwargs):
    out = fn(params, **kwargs)
    assert isinstance(out, str), "tool handlers must return JSON-encoded strings"
    return json.loads(out)


class TestFetchCiLogs:
    def test_happy_path_returns_filtered_failure(self, gh_transport, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        data = _call(
            handlers.fetch_ci_logs,
            {"build_id": RUN_ID, "repo": REPO},
            transport=gh_transport,
        )
        assert data["run"]["conclusion"] == "failure"
        assert [j["name"] for j in data["failed_jobs"]] == ["e2e-tests"]
        assert data["failed_jobs"][0]["failed_steps"] == ["Run Playwright tests"]
        assert "TimeoutError" in data["filtered_log"]
        assert "flaky-selector.spec.ts" in data["filtered_log"]
        assert len(data["filtered_log"].encode()) <= 16 * 1024
        assert data["bytes_raw"] > 1_000_000
        assert data["bytes_filtered"] < data["bytes_raw"]

    def test_missing_params_is_json_error(self):
        data = _call(handlers.fetch_ci_logs, {"build_id": RUN_ID})
        assert "required" in data["error"]

    def test_params_as_json_string_accepted(self, gh_transport, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        data = _call(
            handlers.fetch_ci_logs,
            json.dumps({"build_id": RUN_ID, "repo": REPO}),
            transport=gh_transport,
        )
        assert data["run"]["id"] == 9876543210

    def test_no_token_is_json_error(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        data = _call(handlers.fetch_ci_logs, {"build_id": RUN_ID, "repo": REPO})
        assert "GITHUB_TOKEN" in data["error"]

    def test_http_error_is_json_error(self, gh_transport, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        data = _call(
            handlers.fetch_ci_logs,
            {"build_id": "31337", "repo": REPO},
            transport=gh_transport,
        )
        assert "GitHub API error" in data["error"]
        assert data["status"] == 404


class TestAnalyzePlaywrightTrace:
    def test_happy_path_selector_fixture(self):
        data = _call(
            handlers.analyze_playwright_trace,
            {"trace_path": str(FIXTURES / "traces" / "selector.trace.zip")},
        )
        assert data["diagnosis"]["cause"] == "fragile_selector"
        assert data["diagnosis"]["recommended_strategy"] == "testid_selector"
        assert data["trace"]["failing_action"] == "click"
        assert data["trace"]["selector"] == "#btn-1f9c"

    def test_missing_param_is_json_error(self):
        data = _call(handlers.analyze_playwright_trace, {})
        assert "required" in data["error"]

    def test_corrupt_trace_is_json_error(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"junk")
        data = _call(handlers.analyze_playwright_trace, {"trace_path": str(bad)})
        assert "trace.zip" in data["error"]

    def test_ambiguous_trace_uses_ctx_llm(self, fake_ctx):
        fake_ctx.llm.queue(
            {
                "cause": "data",
                "triage_label": "data",
                "evidence": "checksum mismatch",
                "confidence": 0.85,
            }
        )
        data = _call(
            handlers.analyze_playwright_trace,
            {"trace_path": str(FIXTURES / "traces" / "ambiguous-synthetic.trace.zip")},
            ctx=fake_ctx,
        )
        assert data["diagnosis"]["cause"] == "data"
        assert data["diagnosis"]["llm_used"] is True
        assert len(fake_ctx.llm.structured_calls) == 1

    def test_high_confidence_does_not_touch_ctx_llm(self, fake_ctx):
        data = _call(
            handlers.analyze_playwright_trace,
            {"trace_path": str(FIXTURES / "traces" / "race.trace.zip")},
            ctx=fake_ctx,
        )
        assert data["diagnosis"]["cause"] == "race_condition"
        assert fake_ctx.llm.structured_calls == []


class TestHealFlakyTest:
    SELECTOR_TRACE = str(FIXTURES / "traces" / "selector.trace.zip")

    @staticmethod
    def _flaky_then_green():
        from conftest import FakeSandbox, make_run_report

        return FakeSandbox(
            scripted=[make_run_report(5, failures=2), make_run_report(10, failures=0)]
        )

    def test_missing_params_is_json_error(self):
        data = _call(handlers.heal_flaky_test, {"repo_dir": "x"})
        assert "required" in data["error"]

    def test_unknown_mode_is_json_error(self):
        data = _call(
            handlers.heal_flaky_test,
            {"repo_dir": "x", "test_id": "y", "mode": "yolo"},
        )
        assert "unknown mode" in data["error"]

    def test_suggest_mode_returns_diff_and_no_dispatches(self, fake_ctx):
        from conftest import TOY_APP

        data = _call(
            handlers.heal_flaky_test,
            {
                "repo_dir": str(TOY_APP),
                "test_id": "tests/flaky-selector.spec.ts",
                "trace_path": self.SELECTOR_TRACE,
                "mode": "suggest",
            },
            ctx=fake_ctx,
            sandbox=self._flaky_then_green(),
        )
        report = data["report"]
        assert report["stable"] is True
        assert report["strategy"] == "testid_selector"
        assert ".getByTestId('submit')" in report["diff"]
        assert "pr" not in data
        assert fake_ctx.dispatched == [], "suggest mode must not touch the write path"

    def test_pr_mode_emits_exact_dispatch_sequence(self, fake_ctx):
        from conftest import TOY_APP, FakeGitHost

        fake_ctx.dispatch_executor = FakeGitHost()
        data = _call(
            handlers.heal_flaky_test,
            {
                "repo_dir": str(TOY_APP),
                "test_id": "tests/flaky-selector.spec.ts",
                "trace_path": self.SELECTOR_TRACE,
                "mode": "pr",
            },
            ctx=fake_ctx,
            sandbox=self._flaky_then_green(),
        )
        assert data["report"]["stable"] is True
        pr = data["pr"]
        assert pr is not None
        import re

        assert re.fullmatch(r"fix/flaky-flaky-selector-[0-9a-f]{8}", pr["branch"])

        # everything went through dispatch_tool, in order, and nothing else:
        # 3 read-only preflight steps, 4 write steps, then the PR tool
        tools = [d["tool"] for d in fake_ctx.dispatched]
        assert tools == ["terminal"] * 7 + ["create_pull_request"]
        pre = [d["arguments"]["command"] for d in fake_ctx.dispatched[:3]]
        assert "status --porcelain" in pre[0]
        assert "rev-parse HEAD" in pre[1]
        assert "rev-parse --abbrev-ref HEAD" in pre[2]
        cmds = [d["arguments"].get("command", "") for d in fake_ctx.dispatched[3:7]]
        assert "checkout -B" in cmds[0] and pr["branch"] in cmds[0]
        assert "apply --index" in cmds[1]
        assert "commit -m" in cmds[2]
        assert f"push -u origin {pr['branch']}" in cmds[3]
        assert all("push -u origin main" not in c for c in cmds)
        pr_args = fake_ctx.dispatched[7]["arguments"]
        assert pr_args["head"] == pr["branch"] and pr_args["base"] == "main"
        # the PR body records the commit burn-in actually validated
        assert pr["validated_head"] == "a1" * 20
        assert "a1" * 20 in pr_args["body"]

    def test_pr_mode_refused_when_burnin_unstable(self, fake_ctx):
        from conftest import TOY_APP, FakeSandbox, make_run_report

        data = _call(
            handlers.heal_flaky_test,
            {
                "repo_dir": str(TOY_APP),
                "test_id": "tests/flaky-selector.spec.ts",
                "trace_path": self.SELECTOR_TRACE,
                "mode": "pr",
            },
            ctx=fake_ctx,
            sandbox=FakeSandbox(
                scripted=[make_run_report(5, failures=2), make_run_report(10, failures=1)]
            ),
        )
        assert data["report"]["stable"] is False
        assert data["pr"] is None
        assert "refusing" in data["pr_skipped"]
        assert fake_ctx.dispatched == [], "an unstable heal must never reach the write path"

    def test_pr_mode_without_ctx_is_error(self):
        data = _call(
            handlers.heal_flaky_test,
            {"repo_dir": "x", "test_id": "y", "mode": "pr"},
        )
        assert "approval pipeline" in data["error"]

    def test_run_recorded_in_store(self, fake_ctx, tmp_path):
        from conftest import TOY_APP
        from flaky_healer.recipes.store import HealerStore

        store = HealerStore(tmp_path / "healer.db")
        _call(
            handlers.heal_flaky_test,
            {
                "repo_dir": str(TOY_APP),
                "test_id": "tests/flaky-selector.spec.ts",
                "trace_path": self.SELECTOR_TRACE,
            },
            ctx=fake_ctx,
            sandbox=self._flaky_then_green(),
            store=store,
        )
        runs = store.runs()
        assert len(runs) == 1
        assert runs[0]["result"] == "stable"
        assert runs[0]["source"] == "local"


class TestObserverHooks:
    def test_hooks_never_raise_even_on_weird_payloads(self):
        handlers.on_pre_approval_request({"tool": "terminal"}, session=object())
        handlers.on_post_approval_response(object(), approved=True)

    def test_simulated_approval_events_write_audit_rows(self, fake_ctx, plugin_module):
        plugin_module.register(fake_ctx)
        fake_ctx.fire_hook(
            "pre_approval_request", {"tool": "terminal", "command": "git push"}
        )
        fake_ctx.fire_hook("post_approval_response", {"approved": True})

        rows = handlers._store_for().audit_rows()
        events = [r["event"] for r in rows]
        assert "pre_approval_request" in events
        assert "post_approval_response" in events
        pre = next(r for r in rows if r["event"] == "pre_approval_request")
        assert pre["payload"]["args"][0]["command"] == "git push"
