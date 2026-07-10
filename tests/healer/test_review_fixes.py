"""Regression tests for the audit-review fixes (bugs found in code review).

Each test pins a specific defect so it cannot silently come back. Grouped by the
review id (H = high, M = medium, L = low) it closes.
"""

import json
import subprocess
import urllib.error
import urllib.request
import zipfile

import handlers
import pytest
from conftest import FIXTURES, TOY_APP, FakeSandbox, FakeTransport
from flaky_healer.ci.base import UrllibTransport
from flaky_healer.ci.github_actions import GitHubActionsCI
from flaky_healer.diagnose import diagnose
from flaky_healer.healer import heal
from flaky_healer.logfilter import filter_log
from flaky_healer.shapes import message_shape, selector_shape
from flaky_healer.strategies import apply_ops, get_strategy, unified_diff
from flaky_healer.strategies.base import PatchOp
from flaky_healer.trace import parse_trace

TRACES = FIXTURES / "traces"


# --- H1: path traversal via test_id ----------------------------------------


class TestPathTraversal:
    @pytest.mark.parametrize(
        "bad", ["/etc/passwd", "../../../../etc/passwd", "tests/../../../etc/passwd"]
    )
    def test_absolute_or_escaping_test_id_is_refused(self, bad):
        report = heal(repo_dir=TOY_APP, test_id=bad, sandbox=FakeSandbox())
        assert "must be a relative path inside repo_dir" in report.error
        assert report.stable is False

    def test_apply_ops_refuses_target_outside_root(self, tmp_path):
        outside = tmp_path.parent / "escaped.txt"
        outside.write_text("x\n")
        op = PatchOp(file=str(outside), op="replace", anchor="x", old="x", new="y")
        from flaky_healer.strategies import StrategyError

        with pytest.raises(StrategyError, match="escapes the project root"):
            apply_ops(tmp_path, [op])
        assert outside.read_text() == "x\n", "the outside file must be untouched"


# --- H2: token must not survive the cross-host logs redirect ----------------


def test_authorization_is_sent_as_unredirected_header(monkeypatch):
    captured = {}

    class _Resp:
        status = 200
        headers: dict = {}

        def __init__(self):
            self._data = b"{}"

        def read(self, n=None):
            # honor the transport's capped chunked-read contract: drain, then EOF
            data, self._data = self._data, b""
            return data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        captured["req"] = req
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    UrllibTransport().request(
        "GET",
        "https://api.github.com/x",
        headers={"Authorization": "Bearer secret-token", "Accept": "application/json"},
    )
    req = captured["req"]
    # Unredirected headers are NOT replayed by urllib's redirect handler, so the
    # token can never reach the blob-storage host the run-logs endpoint 302s to.
    assert req.unredirected_hdrs.get("Authorization") == "Bearer secret-token"
    assert "Authorization" not in req.headers
    assert req.headers.get("Accept") == "application/json"


# --- H3: network errors become CIError, never escape ------------------------


def test_network_error_is_converted_to_cierror():
    from flaky_healer.ci.base import CIError

    class _BoomTransport:
        def request(self, *a, **k):
            raise urllib.error.URLError("connection refused")

    ci = GitHubActionsCI(token="tok", transport=_BoomTransport())
    with pytest.raises(CIError, match="failed"):
        ci.get_run("a/b", "1")


def test_fetch_ci_logs_returns_json_on_network_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    # https:// (the base is now https-only) at a dead port → a network failure
    # surfaced as a JSON error object, not a raised exception.
    monkeypatch.setenv("FLAKY_HEALER_GITHUB_API", "https://127.0.0.1:9")
    data = json.loads(handlers.fetch_ci_logs({"build_id": "1", "repo": "a/b"}))
    assert "error" in data  # a JSON error object, not a raised exception


def test_heal_returns_json_on_unknown_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("FLAKY_HEALER_SANDBOX", "nope")
    (tmp_path / "t.spec.ts").write_text("test('x', async () => {});\n")
    data = json.loads(handlers.heal_flaky_test({"repo_dir": str(tmp_path), "test_id": "t.spec.ts"}))
    assert "error" in data and "SandboxError" in data["error"]


# --- H3: trace parser tolerates malformed events ----------------------------


def test_trace_parser_survives_wrongly_typed_events(tmp_path):
    events = [
        {"version": 8, "type": "context-options"},
        5,  # a valid-JSON non-dict line
        {"type": "before", "callId": "c1", "class": "Frame", "method": "click",
         "params": "oops"},  # params not a dict
        {"type": "after", "callId": "c1", "error": "boom",  # error not a dict
         "result": {"log": "waiting"}},  # log a string, must not be char-iterated
        {"type": "frame-snapshot", "snapshot": ["not", "a", "dict"]},
    ]
    path = tmp_path / "weird.trace.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0-trace.trace", "\n".join(json.dumps(e) for e in events) + "\n")
    summary = parse_trace(path)  # must not raise
    action = summary.actions[0]
    assert action.api_name == "click"
    assert action.call_log == [], "a string result.log must not be iterated char-by-char"


# --- H3 / M6: LLM diagnosis sanitization ------------------------------------


@pytest.mark.parametrize("bad_conf", ["high", None, [0.5]])
def test_bad_confidence_does_not_crash(bad_conf):
    diag = diagnose(
        trace=None,
        log_excerpt="ambiguous",
        complete_structured=lambda **k: {
            "cause": "timeout", "triage_label": "timeout", "evidence": "e",
            "confidence": bad_conf,
        },
    )
    assert diag["confidence"] == 0.5


def test_non_string_selector_from_llm_does_not_crash():
    diag = diagnose(
        trace=None,
        log_excerpt="race",
        complete_structured=lambda **k: {
            "cause": "race_condition", "triage_label": "flaky", "evidence": "e",
            "confidence": 0.9, "selector": 123,
        },
    )
    assert diag["selector"] is None


def test_rejected_llm_payload_keeps_heuristic_provenance():
    diag = diagnose(
        trace=None,
        log_excerpt="no rule matches this",
        complete_structured=lambda **k: {"cause": "not-a-cause", "confidence": 0.9},
    )
    assert diag["diagnosis_stage"] == "heuristic"
    assert diag["llm_used"] is False
    assert diag["llm_rejected"] is True


# --- H3 / L3: shapes --------------------------------------------------------


def test_message_shape_handles_whitespace_only():
    assert message_shape("   ") == ""
    assert message_shape(" \n ") == ""


def test_numeric_selector_shape_is_length_independent():
    assert selector_shape("#btn-99") == selector_shape("#btn-100") == "#btn-<n>"
    assert selector_shape("#btn-1f9c") == "#btn-<h>", "letters+digits still hash as hex"


# --- H4: diff applies for files without a trailing newline ------------------


def test_diff_applies_for_file_without_trailing_newline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    spec = repo / "t.spec.ts"
    original = "line one\nawait page.click('#x', { timeout: 2000 });"  # no EOF newline
    spec.write_text(original)
    op = PatchOp(file="t.spec.ts", op="replace",
                 anchor="await page.click('#x', { timeout: 2000 });",
                 old="timeout: 2000", new="timeout: 6000")
    diff = unified_diff(apply_ops(repo, [op]))
    assert "\\ No newline at end of file" in diff
    spec.write_text(original)  # restore, then apply the generated patch via git
    (repo / "h.patch").write_text(diff)
    proc = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", "h.patch"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


# --- M1: numeric separators in bump_timeout ---------------------------------


@pytest.mark.parametrize(
    ("source_value", "expected_new"),
    [("25_000", "timeout: 60000"), ("2_500", "timeout: 7500"), ("12_000", "timeout: 36000")],
)
def test_bump_timeout_handles_numeric_separators(source_value, expected_new):
    strategy = get_strategy("bump_timeout")
    src = f"await expect(page.locator('#x')).toBeVisible({{ timeout: {source_value} }});"
    ops = strategy.plan(src, {"cause": "timeout", "selector": "#x"}, None, "t.ts")
    assert ops[0].new == expected_new


# --- M2: compound selector must not mis-resolve a testid --------------------


def test_compound_selector_declines_testid_rewrite(tmp_path):
    from flaky_healer.trace import TraceSummary, _match_testid_for_selector

    summary = TraceSummary()
    summary.dom_ids = {
        "app": {"data-testid": "app-root"},
        "submit-1f9c": {"data-testid": "submit-button"},
    }
    assert _match_testid_for_selector(summary, "#app #submit-1f9c") is None
    assert _match_testid_for_selector(summary, "#submit-1f9c") == "submit-button"


# --- M3: oversized failure block is truncated, not dropped ------------------


def test_oversized_block_is_truncated_not_dropped():
    lines = [f"FAILED case {i}: assertion mismatch on the data grid row {i}" for i in range(2000)]
    out = filter_log("\n".join(lines), context=5, cap_bytes=15 * 1024)
    assert out.blocks_kept == 1
    assert "FAILED case 0:" in out.text, "the failure region head must survive the cap"
    assert out.bytes_filtered <= 16 * 1024


# --- M4: empty diff refuses a PR --------------------------------------------


def test_pr_mode_refused_on_empty_diff(fake_ctx, monkeypatch):
    # force a stable report whose diff is empty by stubbing heal()
    from flaky_healer.healer import HealReport

    def fake_heal(**kwargs):
        return HealReport(
            test_id=kwargs["test_id"], repo_dir=str(kwargs["repo_dir"]),
            strategy="bump_timeout", stable=True, diff="   ",
            burnin={"runs": 10, "passed": 10}, isolation="fake",
            diagnosis={"cause": "timeout"},
        )

    monkeypatch.setattr(handlers.healer, "heal", fake_heal)
    data = json.loads(
        handlers.heal_flaky_test(
            {"repo_dir": str(TOY_APP), "test_id": "tests/stable.spec.ts", "mode": "pr"},
            ctx=fake_ctx,
        )
    )
    assert data["pr"] is None
    assert "no diff" in data["pr_skipped"]
    assert fake_ctx.dispatched == []


def test_pr_branch_is_cut_from_validated_head(tmp_path):
    """The branch is cut from the CURRENT HEAD — the commit burn-in actually
    validated — never from base, which stays only the PR target (a cut from
    base could publish a never-validated combination)."""
    from flaky_healer.gitflow import branch_name, build_dispatches

    branch = branch_name("tests/x.spec.ts", "deadbeefcafe")
    dispatches = build_dispatches(
        repo_dir=tmp_path, branch=branch, base="main",
        patch_path=tmp_path / "p.patch", commit_message="m", pr_title="t", pr_body="b",
    )
    assert dispatches[0]["arguments"]["command"].endswith(f"checkout -B {branch}")
    assert dispatches[-1]["arguments"]["base"] == "main", "base is only the PR target"


# --- M5: audit hook writes to the same store as tool calls ------------------


def test_audit_hook_uses_ctx_store(fake_ctx, monkeypatch):
    monkeypatch.delenv("FLAKY_HEALER_DATA_DIR", raising=False)  # let ctx.hermes_home decide
    handlers.on_pre_approval_request({"tool": "terminal", "command": "git push"}, ctx=fake_ctx)
    store = handlers._store_for(fake_ctx)
    events = [r["event"] for r in store.audit_rows()]
    assert "pre_approval_request" in events


# --- L5: get_jobs paginates -------------------------------------------------


def test_get_jobs_follows_pagination():
    base = "https://api.github.com"
    repo, run = "a/b", "1"
    page1 = {"total_count": 101, "jobs": [{"id": i, "name": f"j{i}"} for i in range(100)]}
    page2 = {"total_count": 101, "jobs": [{"id": 100, "name": "j100"}]}
    t = FakeTransport()
    t.add("GET", f"{base}/repos/{repo}/actions/runs/{run}/jobs?per_page=100&page=1", page1)
    t.add("GET", f"{base}/repos/{repo}/actions/runs/{run}/jobs?per_page=100&page=2", page2)
    ci = GitHubActionsCI(token="tok", transport=t)
    jobs = ci.get_jobs(repo, run)
    assert len(jobs["jobs"]) == 101
    assert jobs["jobs"][-1]["name"] == "j100"


# --- L7: callId-less before events don't create phantom duplicates ----------


def test_callid_less_before_events_are_skipped(tmp_path):
    events = [
        {"version": 8, "type": "context-options"},
        {"type": "before", "class": "Frame", "method": "click", "params": {}},
        {"type": "before", "class": "Frame", "method": "click", "params": {}},
    ]
    path = tmp_path / "noid.trace.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0-trace.trace", "\n".join(json.dumps(e) for e in events) + "\n")
    summary = parse_trace(path)
    assert summary.actions == [], "events without a callId cannot be correlated; skip them"


# --- L8: secret-looking keys are redacted in the audit trail ----------------


def test_audit_payload_redacts_secret_keys(fake_ctx):
    handlers.on_pre_approval_request(
        # obvious placeholder, not a real token (pragma: allowlist secret)
        {"tool": "terminal", "GITHUB_TOKEN": "ghp_EXAMPLE_not_a_real_token", "command": "git push"},
        ctx=fake_ctx,
    )
    rows = handlers._store_for(fake_ctx).audit_rows()
    payload = rows[0]["payload"]["args"][0]
    assert payload["GITHUB_TOKEN"] == "***"
    assert payload["command"] == "git push", "non-secret fields stay readable"
