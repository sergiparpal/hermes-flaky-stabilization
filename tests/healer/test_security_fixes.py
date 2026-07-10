"""Regression tests for the security-audit remediations.

Grouped by finding id (H = high, M = medium, L = low). Each test pins a specific
fix so it cannot silently regress.
"""

import io
import json
import zipfile

import handlers
import pytest
from conftest import FIXTURES, TOY_APP, FakeSandbox, FakeTransport, make_run_report
from flaky_healer import config, redact, zipsafe
from flaky_healer.ci.base import CIError
from flaky_healer.ci.github_actions import GitHubActionsCI
from flaky_healer.sandbox.docker import DockerSandbox
from flaky_healer.strategies import get_strategy
from flaky_healer.trace import TraceSummary

SELECTOR_TRACE = str(FIXTURES / "traces" / "selector.trace.zip")


# --- H1: code injection via an unsanitized data-testid ----------------------


class TestTestidInjection:
    INJECTIONS = [
        "x'); require('child_process').execSync('id'); ('",  # quote break + JS
        'y"); process.exit(1); ("',  # double-quote break
        "a'+b",  # concat break-out
        "id\nawait evil()",  # newline-injected statement
        "id\\",  # trailing backslash
        "has space (paren)",  # parenthesis
        "",  # empty
    ]

    @pytest.mark.parametrize("testid", INJECTIONS)
    def test_unsafe_testid_is_refused(self, testid):
        strat = get_strategy("testid_selector")
        trace = TraceSummary(selector="#btn", testid_on_target=testid)
        src = "await page.locator('#btn').click();"
        assert strat.plan(src, {"cause": "fragile_selector"}, trace, "t.spec.ts") == [], (
            f"a patch must NOT be synthesized from unsafe testid {testid!r}"
        )

    @pytest.mark.parametrize("testid", ["submit", "user-name", "nav.home", "item-count", "a/b"])
    def test_safe_testid_still_rewrites(self, testid):
        strat = get_strategy("testid_selector")
        trace = TraceSummary(selector="#btn", testid_on_target=testid)
        src = "await page.locator('#btn').click();"
        ops = strat.plan(src, {"cause": "fragile_selector"}, trace, "t.spec.ts")
        assert ops and ops[0].new == f".getByTestId('{testid}')"

    def test_injection_never_reaches_the_diff_end_to_end(self, fake_ctx):
        """A trace carrying a malicious testid heals to no patch, never a diff
        that embeds the payload."""
        from flaky_healer.healer import heal

        malicious = TraceSummary(
            selector="#btn-1f9c",
            testid_on_target="x'); require('child_process').execSync('touch /tmp/pwned'); ('",
        )
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-selector.spec.ts",
            trace_summary=malicious,
            strategy_override="testid_selector",
            sandbox=FakeSandbox(),
        )
        assert "execSync" not in report.diff
        assert report.diff == ""


# --- M1: decompression-bomb (zip-bomb) caps ---------------------------------


class TestZipBomb:
    def test_budget_rejects_oversized_entry(self, tmp_path):
        path = tmp_path / "b.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.trace", "A" * 200_000)  # compresses tiny, expands large
        with zipfile.ZipFile(path) as zf:
            budget = zipsafe.ZipBudget(max_total=10_000, max_entry=10_000)
            with pytest.raises(zipsafe.ZipLimitError):
                budget.read(zf, "big.trace")

    def test_budget_enforces_cumulative_total(self, tmp_path):
        path = tmp_path / "c.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("a.trace", "A" * 4_000)
            zf.writestr("b.trace", "B" * 4_000)
        with zipfile.ZipFile(path) as zf:
            budget = zipsafe.ZipBudget(max_total=5_000, max_entry=10_000)
            budget.read(zf, "a.trace")  # 4000 ok
            with pytest.raises(zipsafe.ZipLimitError):
                budget.read(zf, "b.trace")  # would exceed the 5000 cumulative cap

    def test_guard_entry_count(self, tmp_path):
        path = tmp_path / "many.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for i in range(20):
                zf.writestr(f"f{i}.txt", "x")
        with zipfile.ZipFile(path) as zf:
            zipsafe.guard_entry_count(zf, limit=100)  # under cap: fine
            with pytest.raises(zipsafe.ZipLimitError):
                zipsafe.guard_entry_count(zf, limit=10)

    def test_parse_trace_surfaces_bomb_as_value_error(self, tmp_path, monkeypatch):
        from flaky_healer import trace as trace_mod

        monkeypatch.setattr(
            trace_mod, "ZipBudget", lambda: zipsafe.ZipBudget(max_total=5_000, max_entry=5_000)
        )
        bomb = tmp_path / "bomb.trace.zip"
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("0-trace.trace", "X" * 50_000)
        with pytest.raises(ValueError, match="rejected"):
            trace_mod.parse_trace(bomb)

    def test_logs_zip_to_text_enforces_cap(self, monkeypatch):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("0_job/1_step.txt", "Z" * 60_000)
        orig = zipsafe.ZipBudget  # capture before patching the shared module attr
        monkeypatch.setattr(
            handlers.zipsafe, "ZipBudget", lambda: orig(max_total=5_000, max_entry=5_000)
        )
        with pytest.raises(zipsafe.ZipLimitError):
            handlers._logs_zip_to_text(buf.getvalue())


class TestZipBudgetStreaming:
    """The streaming line reader must enforce exactly the same caps as read()."""

    @staticmethod
    def _zip(tmp_path, name, content, *, deflate=False):
        path = tmp_path / "z.zip"
        comp = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
        with zipfile.ZipFile(path, "w", comp) as zf:
            zf.writestr(name, content)
        return path

    def test_yields_each_line(self, tmp_path):
        path = self._zip(tmp_path, "m.trace", "a\nb\nc\n")
        with zipfile.ZipFile(path) as zf:
            assert list(zipsafe.ZipBudget().iter_lines(zf, "m.trace")) == ["a", "b", "c"]

    def test_keeps_final_unterminated_line(self, tmp_path):
        path = self._zip(tmp_path, "m.trace", "a\nb")
        with zipfile.ZipFile(path) as zf:
            assert list(zipsafe.ZipBudget().iter_lines(zf, "m.trace")) == ["a", "b"]

    def test_enforces_entry_cap(self, tmp_path):
        path = self._zip(tmp_path, "big.trace", "A" * 200_000, deflate=True)
        with zipfile.ZipFile(path) as zf:
            budget = zipsafe.ZipBudget(max_total=10_000, max_entry=10_000)
            with pytest.raises(zipsafe.ZipLimitError):
                list(budget.iter_lines(zf, "big.trace"))

    def test_charges_cumulative_budget_across_members(self, tmp_path):
        path = tmp_path / "two.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.trace", "x\n" * 100)  # 200 bytes
            zf.writestr("b.trace", "y\n" * 100)  # 200 bytes
        with zipfile.ZipFile(path) as zf:
            budget = zipsafe.ZipBudget(max_total=300, max_entry=10_000)
            assert len(list(budget.iter_lines(zf, "a.trace"))) == 100
            assert budget.remaining == 100  # 300 - 200 consumed
            with pytest.raises(zipsafe.ZipLimitError):
                list(budget.iter_lines(zf, "b.trace"))  # 200 > 100 remaining

    def test_decodes_multibyte_split_across_chunks(self, tmp_path):
        # 'é' is two UTF-8 bytes; an odd chunk size slices some sequences in half
        path = self._zip(tmp_path, "m.trace", "é" * 500 + "\n")
        with zipfile.ZipFile(path) as zf:
            lines = list(zipsafe.ZipBudget().iter_lines(zf, "m.trace", chunk_size=7))
            assert lines == ["é" * 500]


# --- M2: docker sandbox hardening + digest pin ------------------------------


class TestDockerHardening:
    def test_hardening_flags_present(self, tmp_path):
        cmd = DockerSandbox(image="img").build_command(tmp_path, "t.spec.ts", "c")
        assert "--cap-drop=ALL" in cmd
        assert "--security-opt=no-new-privileges" in cmd
        assert "--read-only" in cmd
        assert any(a.startswith("--tmpfs=/tmp") for a in cmd), cmd
        # hardening flags precede the image (which precedes the command)
        assert cmd.index("--read-only") < cmd.index("img") < cmd.index("npx")

    def test_default_image_is_digest_pinned(self):
        assert "@sha256:" in config.docker_image()


# --- M3: repo / run_id validation + encoding, http(s) base ------------------


class TestGitHubInputValidation:
    @pytest.mark.parametrize(
        "bad", ["no-slash", "a/b/c", "../etc", "owner/..", "../x", "a b/c", "a/b?x=1", "a/b#f", ""]
    )
    def test_invalid_repo_rejected(self, bad):
        ci = GitHubActionsCI(token="t", transport=FakeTransport())
        with pytest.raises(CIError, match="invalid repo|traversal"):
            ci.get_run(bad, "1")

    @pytest.mark.parametrize("bad", ["abc", "1 2", "1;2", "1/../2", "12#", "-5"])
    def test_invalid_run_id_rejected(self, bad):
        ci = GitHubActionsCI(token="t", transport=FakeTransport())
        with pytest.raises(CIError, match="invalid run id"):
            ci.get_run("a/b", bad)

    def test_non_http_base_rejected(self):
        with pytest.raises(CIError, match="http"):
            GitHubActionsCI(token="t", api_base="file:///etc/passwd")

    def test_valid_inputs_pass(self):
        t = FakeTransport()
        t.add("GET", "https://api.github.com/repos/acme/web-shop/actions/runs/42", {"id": 42})
        ci = GitHubActionsCI(token="t", transport=t)
        assert ci.get_run("acme/web-shop", "42") == {"id": 42}


# --- L1: value-level token redaction in the audit trail ---------------------


class TestValueRedaction:
    REAL_LOOKING = "ghp_" + "a" * 36  # shape of a real classic PAT (pragma: allowlist secret)

    def test_token_in_command_string_is_masked(self):
        out = redact.redact({"command": f"git push https://{self.REAL_LOOKING}@github.com"})
        assert self.REAL_LOOKING not in out["command"]
        assert "***" in out["command"]

    def test_bearer_value_is_masked(self):
        out = redact.redact({"header": "Authorization: Bearer sk-livetoken-abcdef123456"})
        assert "sk-livetoken-abcdef123456" not in out["header"]

    def test_token_in_positional_string_is_masked(self):
        out = redact.redact([f"deploy with {self.REAL_LOOKING}"])
        assert self.REAL_LOOKING not in out[0]

    def test_plain_text_is_left_alone(self):
        assert redact.redact({"command": "git push origin main"}) == {
            "command": "git push origin main"
        }

    def test_handlers_and_recipe_store_share_one_pattern(self):
        """The token pattern used to be copy-pasted into recipes/store.py with a
        comment noting that importing handlers would cycle. Both consumers must
        now resolve to the SAME compiled object, so a newly-supported credential
        format can never be added to one path and forgotten on the other."""
        from flaky_healer.recipes import store as recipe_store

        assert handlers.redact_mod is redact
        assert recipe_store.redact_mod is redact
        assert handlers.redact_mod.TOKEN_VALUE_RE is recipe_store.redact_mod.TOKEN_VALUE_RE

    def test_repr_fallback_masks_tokens_in_unserializable_payload(self):
        """Both the handler and the store route their json.dumps TypeError
        fallback through repr_fallback; it must mask token-shaped text."""

        class Opaque:
            def __repr__(self):
                return f"<Opaque token={TestValueRedaction.REAL_LOOKING}>"

        out = redact.repr_fallback(Opaque())
        assert self.REAL_LOOKING not in out["repr"]
        assert "***" in out["repr"]
        assert len(out["repr"]) <= redact.AUDIT_REPR_LIMIT


# --- H2: subprocess sandbox is not auto-promoted to a PR --------------------


class TestSubprocessPrGuard:
    @staticmethod
    def _subprocess_sandbox():
        return FakeSandbox(
            scripted=[
                make_run_report(5, failures=2, isolation="subprocess"),
                make_run_report(10, failures=0, isolation="subprocess"),
            ]
        )

    def test_pr_refused_when_validated_in_subprocess(self, fake_ctx):
        data = json.loads(
            handlers.heal_flaky_test(
                {
                    "repo_dir": str(TOY_APP),
                    "test_id": "tests/flaky-selector.spec.ts",
                    "trace_path": SELECTOR_TRACE,
                    "mode": "pr",
                },
                ctx=fake_ctx,
                sandbox=self._subprocess_sandbox(),
            )
        )
        assert data["report"]["stable"] is True
        assert data["report"]["isolation"] == "subprocess"
        assert data["pr"] is None
        assert "subprocess" in data["pr_skipped"]
        assert fake_ctx.dispatched == [], "a subprocess heal must not reach the write path"

    def test_override_env_allows_subprocess_pr(self, fake_ctx, monkeypatch):
        from conftest import FakeGitHost

        monkeypatch.setenv("FLAKY_HEALER_ALLOW_SUBPROCESS_PR", "1")
        fake_ctx.dispatch_executor = FakeGitHost()
        data = json.loads(
            handlers.heal_flaky_test(
                {
                    "repo_dir": str(TOY_APP),
                    "test_id": "tests/flaky-selector.spec.ts",
                    "trace_path": SELECTOR_TRACE,
                    "mode": "pr",
                },
                ctx=fake_ctx,
                sandbox=self._subprocess_sandbox(),
            )
        )
        assert data["pr"] is not None
        assert fake_ctx.dispatched, "override must let the write path proceed"


class TestSubprocessNetNamespace:
    def test_disabled_returns_no_prefix(self, monkeypatch):
        from flaky_healer.sandbox import subproc

        monkeypatch.setenv("FLAKY_HEALER_SUBPROC_ISOLATE_NET", "0")
        subproc._netns_prefix.cache_clear()
        try:
            assert subproc._netns_prefix() == ()
        finally:
            subproc._netns_prefix.cache_clear()

    def test_enabled_is_empty_or_unshare_with_net(self, monkeypatch):
        from flaky_healer.sandbox import subproc

        monkeypatch.setenv("FLAKY_HEALER_SUBPROC_ISOLATE_NET", "1")
        subproc._netns_prefix.cache_clear()
        try:
            prefix = subproc._netns_prefix()
            assert prefix == () or (prefix[0].endswith("unshare") and "--net" in prefix)
        finally:
            subproc._netns_prefix.cache_clear()
