"""Recipe layer tests (plan Phase 5 acceptance).

Core property: healing the same failure twice — the second run consumes 0
LLM-stub calls and skips diagnosis/strategy selection, but STILL executes the
full reproduce + burn-in cycle.
"""

import json

import handlers
import pytest
from conftest import FIXTURES, FakeSandbox, build_synthetic_trace, make_run_report
from flaky_healer.healer import heal
from flaky_healer.recipes import matcher
from flaky_healer.recipes.signature import (
    parts_from_trace,
    relaxed_key,
    signature_hash,
    signature_parts,
)
from flaky_healer.recipes.store import HealerStore
from flaky_healer.skill_export import export_learned_patterns
from flaky_healer.trace import parse_trace

TRACES = FIXTURES / "traces"


@pytest.fixture
def store(tmp_path):
    return HealerStore(tmp_path / "data" / "healer.db")


@pytest.fixture
def widget_repo(tmp_path):
    """Tiny project whose spec is healable by bump_timeout (ambiguous trace)."""
    repo = tmp_path / "widget-repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "widget.spec.ts").write_text(
        "import { test } from '@playwright/test';\n\n"
        "test('clicks the widget', async ({ page }) => {\n"
        "  await page.goto('/');\n"
        "  await page.click('#widget', { timeout: 1000 });\n"
        "});\n"
    )
    return repo


class TestSignature:
    def test_rotating_ids_normalize_to_same_signature(self):
        a = signature_parts(
            error_class="TimeoutError", failing_action="click",
            selector="#btn-1f9c", message="Timeout 2000ms exceeded.",
        )
        b = signature_parts(
            error_class="TimeoutError", failing_action="click",
            selector="#btn-59c2", message="Timeout 2100ms exceeded.",
        )
        assert a["selector_shape"] == "#btn-<h>"
        assert signature_hash(a) == signature_hash(b)

    def test_different_error_class_changes_signature(self):
        base = dict(failing_action="click", selector="#x", message="boom")
        a = signature_parts(error_class="TimeoutError", **base)
        b = signature_parts(error_class="AssertionError", **base)
        assert signature_hash(a) != signature_hash(b)

    def test_relaxed_key_ignores_selector_and_message(self):
        a = signature_parts(
            error_class="Error", failing_action="click", selector="#widget", message="m1"
        )
        b = signature_parts(
            error_class="Error", failing_action="click", selector="#gadget", message="m2"
        )
        assert signature_hash(a) != signature_hash(b)
        assert relaxed_key(a) == relaxed_key(b)

    def test_parts_from_real_trace(self):
        parts = parts_from_trace(parse_trace(TRACES / "selector.trace.zip"))
        assert parts["error_class"] == "TimeoutError"
        assert parts["failing_action"] == "click"
        assert parts["selector_shape"] == "#btn-<h>"


class TestMatcher:
    def test_exact_relaxed_miss(self, store):
        parts = signature_parts(
            error_class="Error", failing_action="click", selector="#widget", message="m"
        )
        store.upsert_recipe(
            signature_hash(parts),
            diagnosis={"cause": "timeout"},
            strategy="bump_timeout",
            patch_ops=[],
            signature_parts=parts,
        )
        recipe, kind = matcher.match(store, parts)
        assert kind == "exact" and recipe is not None

        renamed = signature_parts(
            error_class="Error", failing_action="click", selector="#gadget", message="m"
        )
        recipe, kind = matcher.match(store, renamed)
        assert kind == "relaxed" and recipe is not None

        other = signature_parts(
            error_class="AssertionError", failing_action="fill", selector="#x", message="m"
        )
        recipe, kind = matcher.match(store, other)
        assert kind == "miss" and recipe is None


class TestDoubleHealShortCircuit:
    def _llm_stub(self, queue):
        def call(*, instructions, input, schema=None):
            if not queue:
                raise AssertionError("LLM stub consumed more than queued")
            return queue.pop(0)

        return call

    def test_second_heal_skips_reasoning_but_not_burnin(self, store, widget_repo, tmp_path):
        trace = parse_trace(
            build_synthetic_trace(tmp_path / "ambiguous.trace.zip")
        )
        llm_response = {
            "cause": "timeout",
            "triage_label": "timeout",
            "evidence": "widget needs more time to settle",
            "recommended_strategy": "bump_timeout",
            "confidence": 0.9,
        }
        queue = [llm_response]

        first = heal(
            repo_dir=widget_repo,
            test_id="tests/widget.spec.ts",
            trace_summary=trace,
            sandbox=FakeSandbox(
                scripted=[make_run_report(5, failures=2), make_run_report(10, failures=0)]
            ),
            complete_structured=self._llm_stub(queue),
            store=store,
        )
        assert first.error is None
        assert first.stable is True
        assert first.llm_calls == 1, "ambiguous first run must consult the LLM once"
        assert first.diagnosis_stage == "llm"
        assert first.strategy == "bump_timeout"
        assert "timeout: 3000" in first.diff
        recipe = store.get_recipe(first.recipe_signature)
        assert recipe is not None, "successful heal must persist a recipe"

        second_sandbox = FakeSandbox(
            scripted=[make_run_report(5, failures=1), make_run_report(10, failures=0)]
        )
        second = heal(
            repo_dir=widget_repo,
            test_id="tests/widget.spec.ts",
            trace_summary=trace,
            sandbox=second_sandbox,
            complete_structured=self._llm_stub([]),  # raises if consumed at all
            store=store,
        )
        assert second.error is None
        assert second.llm_calls == 0, "recipe hit must consume zero LLM calls"
        assert second.recipe_used is True
        assert second.recipe_match == "exact"
        assert second.diagnosis_stage == "recipe"
        assert second.strategy == "bump_timeout"
        assert second.stable is True
        # validation is never skipped: full reproduce M + burn-in N executed
        assert [c["repeats"] for c in second_sandbox.calls] == [5, 10]

        recipe = store.get_recipe(first.recipe_signature)
        assert (recipe.hits, recipe.successes, recipe.failures) == (1, 1, 0)

    def test_relaxed_match_covers_renamed_selector(self, store, widget_repo, tmp_path):
        original = parse_trace(build_synthetic_trace(tmp_path / "a.trace.zip"))
        first = heal(
            repo_dir=widget_repo,
            test_id="tests/widget.spec.ts",
            trace_summary=original,
            sandbox=FakeSandbox(
                scripted=[make_run_report(5, failures=2), make_run_report(10, failures=0)]
            ),
            complete_structured=self._llm_stub(
                [
                    {
                        "cause": "timeout",
                        "triage_label": "timeout",
                        "evidence": "slow widget",
                        "recommended_strategy": "bump_timeout",
                        "confidence": 0.9,
                    }
                ]
            ),
            store=store,
        )
        assert first.stable is True

        # same error class + failing action, alphabetically different selector
        renamed_repo = widget_repo
        spec = renamed_repo / "tests" / "widget.spec.ts"
        spec.write_text(spec.read_text().replace("#widget", "#gadget"))
        renamed = parse_trace(
            build_synthetic_trace(
                tmp_path / "b.trace.zip",
                selector="#gadget",
                call_log='locator resolved to <div id="gadget">…</div>',
            )
        )
        assert signature_hash(parts_from_trace(renamed)) != first.recipe_signature

        second = heal(
            repo_dir=renamed_repo,
            test_id="tests/widget.spec.ts",
            trace_summary=renamed,
            sandbox=FakeSandbox(
                scripted=[make_run_report(5, failures=1), make_run_report(10, failures=0)]
            ),
            complete_structured=self._llm_stub([]),
            store=store,
        )
        assert second.recipe_used is True
        assert second.recipe_match == "relaxed"
        assert second.llm_calls == 0
        assert second.stable is True
        assert "timeout: 3000" in second.diff
        # the MATCHED recipe's counters were bumped
        original_recipe = store.get_recipe(first.recipe_signature)
        assert original_recipe.hits == 1 and original_recipe.successes == 1


class TestRecipeFailureFallback:
    def test_failed_recipe_burnin_records_failure_and_falls_back(self, store):
        """Seed a wrong recipe (bump_timeout) for the selector fixture whose
        correct fix is testid_selector; the failed burn-in must bump the
        failure counter and the fallback path must heal it."""
        from conftest import TOY_APP

        trace = parse_trace(TRACES / "selector.trace.zip")
        parts = parts_from_trace(trace)
        sig = signature_hash(parts)
        store.upsert_recipe(
            sig,
            diagnosis={"cause": "timeout", "recommended_strategy": "bump_timeout"},
            strategy="bump_timeout",
            patch_ops=[],
            signature_parts=parts,
        )
        sandbox = FakeSandbox(
            scripted=[
                make_run_report(5, failures=2),  # reproduce
                make_run_report(10, failures=4),  # recipe burn-in: fails
                make_run_report(10, failures=0),  # fallback burn-in: green
            ]
        )
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-selector.spec.ts",
            trace_summary=trace,
            sandbox=sandbox,
            store=store,
        )
        assert report.recipe_fallback is True
        assert report.stable is True
        assert report.strategy == "testid_selector"
        assert ".getByTestId('submit')" in report.diff
        assert len(sandbox.calls) == 3, "reproduce once, burn-in twice (recipe + fallback)"

        recipe = store.get_recipe(sig)
        assert recipe.failures == 1, "failed recipe burn-in must be recorded"
        assert recipe.strategy == "testid_selector", "fallback success refreshes the recipe"


class TestSkillExport:
    def test_export_has_agentskills_frontmatter_and_content(self, store):
        parts = signature_parts(
            error_class="TimeoutError", failing_action="click",
            selector="#btn-1f9c", message="Timeout 2000ms exceeded.",
        )
        store.upsert_recipe(
            signature_hash(parts),
            diagnosis={"cause": "fragile_selector", "evidence": "rotating id"},
            strategy="testid_selector",
            patch_ops=[{"file": "t.ts", "op": "replace", "anchor": "a", "old": "x", "new": "y"}],
            signature_parts=parts,
        )
        out = export_learned_patterns(store)
        text = out.read_text()
        assert text.startswith("---\n")
        frontmatter = text.split("---")[1]
        assert "name: flaky-healer-learned-patterns" in frontmatter
        assert "description:" in frontmatter
        assert signature_hash(parts)[:8] in text
        assert "testid_selector" in text
        assert "#btn-<h>" in text

    def test_export_written_after_successful_heal(self, store, widget_repo, tmp_path):
        trace = parse_trace(build_synthetic_trace(tmp_path / "t.trace.zip"))
        heal(
            repo_dir=widget_repo,
            test_id="tests/widget.spec.ts",
            trace_summary=trace,
            diagnosis=None,
            sandbox=FakeSandbox(
                scripted=[make_run_report(5, failures=1), make_run_report(10, failures=0)]
            ),
            complete_structured=lambda **kw: {
                "cause": "timeout",
                "triage_label": "timeout",
                "evidence": "slow",
                "recommended_strategy": "bump_timeout",
                "confidence": 0.9,
            },
            store=store,
        )
        assert (store.db_path.parent / "learned-patterns.md").is_file()


class TestListHealingRecipesHandler:
    def test_lists_recipes_with_stats(self, store):
        parts = signature_parts(
            error_class="TimeoutError", failing_action="click", selector="#x", message="m"
        )
        store.upsert_recipe(
            signature_hash(parts),
            diagnosis={"cause": "timeout"},
            strategy="bump_timeout",
            patch_ops=[],
            signature_parts=parts,
        )
        store.bump_recipe(signature_hash(parts), success=True)
        out = handlers.list_healing_recipes({}, store=store)
        data = json.loads(out)
        assert data["count"] == 1
        recipe = data["recipes"][0]
        assert recipe["strategy"] == "bump_timeout"
        assert recipe["hits"] == 1 and recipe["successes"] == 1
        assert recipe["sig8"] == signature_hash(parts)[:8]

    def test_empty_store_lists_zero(self, store):
        data = json.loads(handlers.list_healing_recipes({}, store=store))
        assert data == {
            "count": 0,
            "recipes": [],
            "db_path": str(store.db_path),
            "learned_patterns_md": str(store.db_path.parent / "learned-patterns.md"),
        }
