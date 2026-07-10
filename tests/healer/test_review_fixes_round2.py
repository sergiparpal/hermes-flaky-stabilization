"""Regression tests for the second review round (verified findings F1-F10).

Each test pins one verified finding — grouped and numbered by the finding it
closes — reproducing the exact reported scenario so the defect cannot silently
come back. Docker-path findings are exercised at the CLI-invocation seam with
fakes (no daemon needed), per the suite's conventions.
"""

import dis
import io
import os
import stat
import subprocess
import sys
import urllib.request

import pytest
from conftest import REPO_ROOT, FakeTransport
from flaky_healer.ci import base as ci_base
from flaky_healer.ci import github_actions as gh_mod
from flaky_healer.ci.base import CIError, UrllibTransport
from flaky_healer.ci.github_actions import GitHubActionsCI
from flaky_healer.recipes import matcher
from flaky_healer.recipes.signature import (
    is_degenerate,
    parts_from_trace,
    signature_hash,
    signature_parts,
)
from flaky_healer.recipes.store import HealerStore
from flaky_healer.sandbox import subproc as subproc_mod
from flaky_healer.sandbox.base import SandboxError, copy_project
from flaky_healer.sandbox.docker import DockerSandbox
from flaky_healer.strategies import PatchOp, apply_ops, get_strategy
from flaky_healer.trace import TraceSummary

# --- F1 (CRITICAL): apply_ops must never write through a hardlinked inode ---


class TestApplyOpsBreaksHardlinks:
    """With FLAKY_HEALER_HARDLINK_COPY=1 the sandbox copy hardlinks the user's
    original files; an in-place write would O_TRUNC the SHARED inode and
    silently rewrite the user's real repo (D6 violation)."""

    ORIGINAL = "await page.click('#x', { timeout: 2000 });\n"
    OP = PatchOp(
        file="tests/t.spec.ts",
        op="replace",
        anchor="timeout: 2000",
        old="timeout: 2000",
        new="timeout: 6000",
    )

    def _hardlinked_sandbox(self, tmp_path):
        original = tmp_path / "user-repo" / "tests" / "t.spec.ts"
        original.parent.mkdir(parents=True)
        original.write_text(self.ORIGINAL)
        sandbox_root = tmp_path / "sandbox"
        (sandbox_root / "tests").mkdir(parents=True)
        os.link(original, sandbox_root / "tests" / "t.spec.ts")
        assert original.stat().st_nlink == 2
        return original, sandbox_root

    def test_original_bytes_unchanged_and_link_broken(self, tmp_path):
        original, sandbox_root = self._hardlinked_sandbox(tmp_path)
        apply_ops(sandbox_root, [self.OP])
        patched = sandbox_root / "tests" / "t.spec.ts"
        assert "timeout: 6000" in patched.read_text()
        assert original.read_bytes() == self.ORIGINAL.encode(), (
            "the user's ORIGINAL file must never change (D6)"
        )
        assert patched.stat().st_nlink == 1, "the patch must land on a NEW inode"
        assert original.stat().st_nlink == 1
        assert patched.stat().st_ino != original.stat().st_ino

    def test_end_to_end_via_hardlink_copy_project(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLAKY_HEALER_HARDLINK_COPY", "1")
        src = tmp_path / "user-repo"
        (src / "tests").mkdir(parents=True)
        original = src / "tests" / "t.spec.ts"
        original.write_text(self.ORIGINAL)
        work = copy_project(src, tmp_path / "work")
        assert original.stat().st_nlink == 2, "sanity: the copy hardlinked the spec"
        apply_ops(work, [self.OP])
        assert original.read_text() == self.ORIGINAL
        assert "timeout: 6000" in (work / "tests" / "t.spec.ts").read_text()

    def test_file_mode_is_preserved(self, tmp_path):
        (tmp_path / "tests").mkdir()
        spec = tmp_path / "tests" / "t.spec.ts"
        spec.write_text(self.ORIGINAL)
        os.chmod(spec, 0o754)
        apply_ops(tmp_path, [self.OP])
        assert stat.S_IMODE(spec.stat().st_mode) == 0o754


# --- F2 (MAJOR): await_state must not insert mid-expression ------------------


class TestAwaitStatePrettierWrapped:
    """The fallback insertion path must anchor at a statement start; inserting
    before the selector line of a Prettier-wrapped multi-line expression
    produces syntactically corrupt test code."""

    DIAG = {"cause": "race_condition", "selector": "#btn"}
    PRETTIER_SOURCE = (
        "test('shows the button', async ({ page }) => {\n"
        "  await page.goto('/');\n"
        "  await expect(\n"
        "    page.locator('#btn')\n"
        "  ).toBeVisible();\n"
        "});\n"
    )

    def test_insertion_anchors_at_enclosing_statement(self):
        strategy = get_strategy("await_state")
        ops = strategy.plan(self.PRETTIER_SOURCE, self.DIAG, None, "t.spec.ts")
        assert len(ops) == 1
        assert ops[0].anchor == "  await expect(", (
            "the insert must anchor at the statement start, not mid-expression"
        )
        assert ops[0].new == "  await page.waitForLoadState('networkidle');"

    def test_patched_source_stays_syntactic(self, tmp_path):
        (tmp_path / "t.spec.ts").write_text(self.PRETTIER_SOURCE)
        strategy = get_strategy("await_state")
        ops = strategy.plan(self.PRETTIER_SOURCE, self.DIAG, None, "t.spec.ts")
        changes = apply_ops(tmp_path, ops)
        lines = changes["t.spec.ts"][1].splitlines()
        idx = lines.index("  await page.waitForLoadState('networkidle');")
        assert lines[idx + 1] == "  await expect(", (
            "the wait must precede the whole statement, never split it"
        )

    def test_top_of_source_multiline_expression(self):
        source = "await expect(\n  page.locator('#btn')\n).toBeVisible();\n"
        ops = get_strategy("await_state").plan(source, self.DIAG, None, "t.spec.ts")
        assert len(ops) == 1
        assert ops[0].anchor == "await expect("

    def test_single_line_target_keeps_direct_anchor(self):
        source = "  await expect(page.locator('#btn')).toBeVisible();\n"
        ops = get_strategy("await_state").plan(source, self.DIAG, None, "t.spec.ts")
        assert ops[0].anchor == "  await expect(page.locator('#btn')).toBeVisible();"

    def test_declines_when_no_safe_anchor_exists(self):
        source = "foo.helper(\n  page.locator('#btn')\n);\n"
        assert get_strategy("await_state").plan(source, self.DIAG, None, "t.spec.ts") == []

    def test_idempotent_after_reanchored_patch(self):
        source = (
            "  await page.waitForLoadState('networkidle');\n"
            "  await expect(\n"
            "    page.locator('#btn')\n"
            "  ).toBeVisible();\n"
        )
        assert get_strategy("await_state").plan(source, self.DIAG, None, "t.spec.ts") == []


# --- F3: multi-line test.setTimeout must decline, not crash ------------------


class TestBumpTimeoutMultilineSetTimeout:
    def test_multiline_set_timeout_declines_instead_of_crashing(self):
        strategy = get_strategy("bump_timeout")
        source = "test.setTimeout(\n  10_000\n);\nawait page.click('#x');\n"
        # pre-fix: \s* matched across the newline and the containing-line lookup
        # raised an uncaught StopIteration, killing the whole heal
        assert strategy.plan(source, {"cause": "timeout"}, None, "t.spec.ts") == []

    def test_single_line_with_inner_spaces_still_bumps(self):
        source = "test.setTimeout( 10_000 );\nawait page.click('#x');\n"
        ops = get_strategy("bump_timeout").plan(source, {"cause": "timeout"}, None, "t.spec.ts")
        assert ops[0].old == "test.setTimeout( 10_000 )"
        assert ops[0].new == "test.setTimeout(30000)"


# --- F4: infra failures must raise SandboxError, not fake test failures ------


class TestDockerInfraExitCodes:
    @staticmethod
    def _fake_run(returncode):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, returncode, stdout="", stderr="docker: Error response from daemon"
            )

        return fake_run

    @pytest.mark.parametrize("code", [125, 126, 127])
    def test_docker_cli_infra_exit_codes_raise(self, monkeypatch, tmp_path, code):
        from flaky_healer.sandbox import docker as docker_mod

        monkeypatch.setattr(docker_mod.subprocess, "run", self._fake_run(code))
        with pytest.raises(SandboxError, match="infrastructure"):
            DockerSandbox(image="img")._run_once(tmp_path, "t.spec.ts", timeout_s=5)

    def test_ordinary_test_failure_still_returns_run_result(self, monkeypatch, tmp_path):
        from flaky_healer.sandbox import docker as docker_mod

        monkeypatch.setattr(docker_mod.subprocess, "run", self._fake_run(1))
        result = DockerSandbox(image="img")._run_once(tmp_path, "t.spec.ts", timeout_s=5)
        assert result.exit_code == 1 and not result.passed


class TestSubprocessLaunchFailure:
    def test_unlaunchable_npx_raises_sandbox_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subproc_mod, "_netns_prefix", lambda: ())

        def boom(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "npx")

        monkeypatch.setattr(subproc_mod.subprocess, "Popen", boom)
        with pytest.raises(SandboxError, match="npx"):
            subproc_mod.SubprocessSandbox()._run_once(tmp_path, tmp_path, "t.spec.ts", 5)


# --- F5: spec files are UTF-8 regardless of the host locale ------------------


def test_apply_ops_reads_utf8_specs_under_c_locale(tmp_path):
    """Exact reproduction: LANG=C host locale + a non-ASCII spec. Run in a
    subprocess because the parent interpreter's locale is fixed at startup."""
    spec = tmp_path / "t.spec.ts"
    spec.write_bytes("await page.click('#botón', { timeout: 2000 });\n".encode())
    code = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from hermes_flaky_stabilization.healer.flaky_healer.strategies import (\n"
        "    PatchOp, apply_ops)\n"
        "op = PatchOp(file='t.spec.ts', op='replace', anchor='timeout: 2000',\n"
        "             old='timeout: 2000', new='timeout: 6000')\n"
        "apply_ops(sys.argv[2], [op])\n"
    )
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
    }
    proc = subprocess.run(
        [sys.executable, "-c", code, str(REPO_ROOT), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"apply_ops must not depend on the locale: {proc.stderr}"
    assert spec.read_text(encoding="utf-8") == (
        "await page.click('#botón', { timeout: 6000 });\n"
    )


# --- F6: degenerate (all-empty) signatures never match or persist ------------


class TestDegenerateSignatures:
    @staticmethod
    def _degenerate():
        return signature_parts(
            error_class=None, failing_action=None, selector=None, message=None
        )

    @staticmethod
    def _real():
        return signature_parts(
            error_class="TimeoutError", failing_action="click", selector="#x", message="m"
        )

    def test_passing_retry_trace_parts_are_degenerate(self):
        # a trace with no failed action and no error event (e.g. a PASSING retry)
        assert is_degenerate(parts_from_trace(TraceSummary()))

    def test_real_failure_parts_are_not_degenerate(self):
        assert not is_degenerate(self._real())

    def test_matcher_misses_on_degenerate_parts(self, tmp_path):
        store = HealerStore(tmp_path / "healer.db")
        degenerate = self._degenerate()
        # seed a recipe UNDER the constant degenerate hash, as a pre-fix run did
        store.upsert_recipe(
            signature_hash(degenerate),
            diagnosis={"cause": "unknown"},
            strategy="bump_timeout",
            patch_ops=[],
            signature_parts=self._real(),
        )
        recipe, kind = matcher.match(store, degenerate)
        assert (recipe, kind) == (None, "miss"), (
            "unrelated failures must never exact-match the constant empty signature"
        )

    def test_store_refuses_degenerate_recipes(self, tmp_path):
        store = HealerStore(tmp_path / "healer.db")
        sig = signature_hash(self._degenerate())
        store.upsert_recipe(
            sig,
            diagnosis={"cause": "unknown"},
            strategy="bump_timeout",
            patch_ops=[],
            signature_parts=self._degenerate(),
        )
        assert store.get_recipe(sig) is None
        assert store.all_recipes() == []

    def test_partsless_legacy_upsert_still_stores(self, tmp_path):
        # signature_parts={} means "no parts recorded" (legacy rows): still
        # stored, with the NULL relaxed key that relaxed matching already skips
        store = HealerStore(tmp_path / "healer.db")
        store.upsert_recipe(
            "legacy-sig", diagnosis={}, strategy="s", patch_ops=[], signature_parts={}
        )
        assert store.get_recipe("legacy-sig") is not None


# --- F7: replacing a recipe body resets its performance counters -------------


class TestRecipeCounterResetOnBodyChange:
    OPS_A = [{"op": "replace", "file": "t.ts", "anchor": "a", "old": "x", "new": "y"}]
    OPS_B = [{"op": "replace", "file": "t.ts", "anchor": "a", "old": "x", "new": "z"}]

    @staticmethod
    def _parts():
        return signature_parts(
            error_class="TimeoutError", failing_action="click", selector="#x", message="m"
        )

    def _seed_and_bump(self, tmp_path):
        store = HealerStore(tmp_path / "healer.db")
        store.upsert_recipe(
            "sig-r",
            diagnosis={"cause": "timeout"},
            strategy="bump_timeout",
            patch_ops=self.OPS_A,
            signature_parts=self._parts(),
        )
        store.bump_recipe("sig-r", success=True)
        store.bump_recipe("sig-r", success=False)
        return store

    def test_changed_patch_ops_reset_counters(self, tmp_path):
        store = self._seed_and_bump(tmp_path)
        store.upsert_recipe(
            "sig-r",
            diagnosis={"cause": "timeout"},
            strategy="bump_timeout",
            patch_ops=self.OPS_B,
            signature_parts=self._parts(),
        )
        recipe = store.get_recipe("sig-r")
        assert (recipe.hits, recipe.successes, recipe.failures) == (0, 0, 0), (
            "a new patch body must not inherit rank earned by the previous patch"
        )
        assert recipe.last_used_ts is None
        assert recipe.patch_ops == self.OPS_B

    def test_changed_strategy_resets_counters(self, tmp_path):
        store = self._seed_and_bump(tmp_path)
        store.upsert_recipe(
            "sig-r",
            diagnosis={"cause": "fragile_selector"},
            strategy="testid_selector",
            patch_ops=self.OPS_A,
            signature_parts=self._parts(),
        )
        recipe = store.get_recipe("sig-r")
        assert (recipe.hits, recipe.successes, recipe.failures) == (0, 0, 0)
        assert recipe.last_used_ts is None

    def test_identical_body_preserves_counters(self, tmp_path):
        store = self._seed_and_bump(tmp_path)
        store.upsert_recipe(
            "sig-r",
            diagnosis={"cause": "timeout", "refreshed": True},
            strategy="bump_timeout",
            patch_ops=self.OPS_A,
            signature_parts=self._parts(),
        )
        recipe = store.get_recipe("sig-r")
        assert (recipe.hits, recipe.successes, recipe.failures) == (2, 1, 1)
        assert recipe.last_used_ts is not None
        assert recipe.diagnosis["refreshed"] is True


# --- F8: the post-kill drain must never hang the heal ------------------------


class TestPostKillCommunicateTimeout:
    class _HungPipeProc:
        """A killed npx whose stdout pipe is kept open by a test-spawned daemon
        that setsid'd away: communicate() without a timeout would block forever."""

        pid = 4242
        returncode = None
        stdin = None
        stderr = None

        def __init__(self):
            self.stdout = io.StringIO()
            self.kills = 0

        def communicate(self, timeout=None):
            if timeout is None:
                pytest.fail("communicate() without a timeout blocks the heal forever")
            raise subprocess.TimeoutExpired(
                cmd="npx playwright test",
                timeout=timeout,
                output="partial output before the hang\n",
            )

        def kill(self):
            self.kills += 1

    def test_escaped_daemon_cannot_hang_the_kill_path(self, monkeypatch, tmp_path):
        proc = self._HungPipeProc()
        monkeypatch.setattr(subproc_mod, "_netns_prefix", lambda: ())
        monkeypatch.setattr(subproc_mod.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(subproc_mod.os, "killpg", lambda pgid, sig: None)
        result = subproc_mod.SubprocessSandbox()._run_once(tmp_path, tmp_path, "t.spec.ts", 1)
        assert result.timed_out and result.exit_code == -1
        assert "partial output" in result.stdout_tail, "collected output must survive"
        assert proc.stdout.closed, "the inherited pipe must be closed, not leaked"


# --- F9: no import between fork and exec -------------------------------------


class TestRlimitsForkSafety:
    def test_resource_is_imported_at_module_level(self):
        assert hasattr(subproc_mod, "resource")
        assert subproc_mod.resource is sys.modules.get("resource")

    def test_rlimits_performs_no_import_in_the_child(self):
        assert not any(
            ins.opname == "IMPORT_NAME" for ins in dis.get_instructions(subproc_mod._rlimits)
        ), "importing between fork and exec in a threaded parent is a deadlock hazard"


# --- F10: the run-logs download is chunked and capped -------------------------


class TestLogsDownloadCap:
    def test_urllib_transport_streams_with_a_byte_cap(self, monkeypatch):
        class _EndlessResp:
            status = 200
            headers: dict = {}

            def __init__(self):
                self.remaining = 10_000

            def read(self, n=None):
                if self.remaining <= 0:
                    return b""
                take = self.remaining if n is None else min(n, self.remaining)
                self.remaining -= take
                return b"x" * take

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(ci_base, "MAX_RESPONSE_BYTES", 4096)
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: _EndlessResp())
        with pytest.raises(CIError, match="cap"):
            UrllibTransport().request("GET", "https://api.github.com/x")

    def test_download_logs_zip_enforces_cap_for_any_transport(self, monkeypatch):
        monkeypatch.setattr(gh_mod, "MAX_LOGS_ZIP_BYTES", 1024)
        t = FakeTransport()
        t.add("GET", "https://api.github.com/repos/a/b/actions/runs/1/logs", b"P" * 2048)
        ci = GitHubActionsCI(token="tok", transport=t)
        with pytest.raises(CIError, match="cap"):
            ci.download_logs_zip("a/b", "1")

    def test_capped_download_still_returns_bytes(self):
        t = FakeTransport()
        t.add("GET", "https://api.github.com/repos/a/b/actions/runs/1/logs", b"PK\x03\x04data")
        ci = GitHubActionsCI(token="tok", transport=t)
        assert ci.download_logs_zip("a/b", "1")[:2] == b"PK"

    def test_default_cap_is_100_mib_and_shared(self):
        assert ci_base.MAX_RESPONSE_BYTES == 100 * 1024 * 1024
        assert gh_mod.MAX_LOGS_ZIP_BYTES == ci_base.MAX_RESPONSE_BYTES
