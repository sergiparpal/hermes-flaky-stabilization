"""Strategy unit tests over the toy specs' real source (pure functions)."""

import pytest
from conftest import FIXTURES, TOY_APP
from flaky_healer.strategies import (
    PatchOp,
    StrategyError,
    apply_ops,
    candidate_strategies,
    get_strategy,
    unified_diff,
)
from flaky_healer.trace import parse_trace

TRACES = FIXTURES / "traces"


def _source(spec: str) -> str:
    return (TOY_APP / "tests" / spec).read_text()


@pytest.fixture
def selector_trace():
    return parse_trace(TRACES / "selector.trace.zip")


@pytest.fixture
def timeout_trace():
    return parse_trace(TRACES / "timeout.trace.zip")


@pytest.fixture
def race_trace():
    return parse_trace(TRACES / "race.trace.zip")


class TestBumpTimeout:
    def test_bumps_explicit_timeout_3x(self, timeout_trace):
        strategy = get_strategy("bump_timeout")
        diag = {"cause": "timeout", "recommended_strategy": "bump_timeout"}
        ops = strategy.plan(
            _source("flaky-timeout.spec.ts"), diag, timeout_trace, "tests/flaky-timeout.spec.ts"
        )
        assert len(ops) == 1
        op = ops[0]
        assert op.op == "replace"
        assert op.old == "timeout: 1500"
        assert op.new == "timeout: 4500"
        assert "#delayed-banner" in op.anchor

    def test_caps_at_60s(self, timeout_trace):
        strategy = get_strategy("bump_timeout")
        source = "await expect(page.locator('#delayed-banner')).toBeVisible({ timeout: 25000 });"
        ops = strategy.plan(source, {"cause": "timeout"}, timeout_trace, "t.spec.ts")
        assert ops[0].new == "timeout: 60000"

    def test_no_bump_when_already_at_cap(self, timeout_trace):
        strategy = get_strategy("bump_timeout")
        source = "await expect(page.locator('#delayed-banner')).toBeVisible({ timeout: 60000 });"
        assert strategy.plan(source, {"cause": "timeout"}, timeout_trace, "t.spec.ts") == []

    def test_falls_back_to_test_set_timeout(self):
        strategy = get_strategy("bump_timeout")
        source = "test.setTimeout(10000);\nawait page.click('#x');"
        ops = strategy.plan(source, {"cause": "timeout", "selector": "#missing"}, None, "t.spec.ts")
        assert ops[0].old == "test.setTimeout(10000)"
        assert ops[0].new == "test.setTimeout(30000)"

    def test_applies_only_to_timeout_or_recommendation(self, timeout_trace):
        strategy = get_strategy("bump_timeout")
        assert strategy.applies({"cause": "timeout"}, timeout_trace)
        assert strategy.applies({"cause": "unknown", "recommended_strategy": "bump_timeout"}, None)
        assert not strategy.applies({"cause": "fragile_selector"}, timeout_trace)


class TestTestidSelector:
    def test_replaces_fragile_locator(self, selector_trace):
        strategy = get_strategy("testid_selector")
        diag = {"cause": "fragile_selector", "recommended_strategy": "testid_selector"}
        ops = strategy.plan(
            _source("flaky-selector.spec.ts"), diag, selector_trace, "tests/flaky-selector.spec.ts"
        )
        assert len(ops) == 1
        assert ops[0].old == ".locator('#btn-1f9c')"
        assert ops[0].new == ".getByTestId('submit')"

    def test_requires_dom_proof(self, timeout_trace):
        """timeout trace: selector resolved, but no shape-matched testid proof needed —
        applies() must hold the strategy back unless testid_on_target exists."""
        strategy = get_strategy("testid_selector")
        diag = {"cause": "fragile_selector"}
        no_proof_trace = parse_trace(TRACES / "ambiguous-synthetic.trace.zip")
        assert no_proof_trace.testid_on_target is None
        assert not strategy.applies(diag, no_proof_trace)
        assert not strategy.applies(diag, None)

    def test_double_quoted_sources_also_match(self, selector_trace):
        strategy = get_strategy("testid_selector")
        source = 'await page.locator("#btn-1f9c").click();'
        ops = strategy.plan(source, {"cause": "fragile_selector"}, selector_trace, "t.spec.ts")
        assert ops[0].new == '.getByTestId("submit")'


class TestAwaitState:
    def test_inserts_networkidle_before_expect(self, race_trace):
        strategy = get_strategy("await_state")
        diag = {"cause": "race_condition", "recommended_strategy": "await_state"}
        source = _source("flaky-race.spec.ts")
        ops = strategy.plan(source, diag, race_trace, "tests/flaky-race.spec.ts")
        assert len(ops) == 1
        op = ops[0]
        assert op.op == "insert_before"
        assert op.new == "  await page.waitForLoadState('networkidle');"
        assert "expect(page.locator('#item-count'))" in op.anchor

    def test_idempotent_when_already_patched(self, race_trace):
        strategy = get_strategy("await_state")
        source = (
            "  await page.waitForLoadState('networkidle');\n"
            "  await expect(page.locator('#item-count')).toHaveText('3 items');\n"
        )
        assert strategy.plan(source, {"cause": "race_condition"}, race_trace, "t.spec.ts") == []


class TestApplyAndDiff:
    def test_apply_ops_and_unified_diff_roundtrip(self, tmp_path, race_trace):
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        spec = repo / "tests" / "flaky-race.spec.ts"
        spec.write_text(_source("flaky-race.spec.ts"))
        strategy = get_strategy("await_state")
        ops = strategy.plan(
            spec.read_text(), {"cause": "race_condition"}, race_trace, "tests/flaky-race.spec.ts"
        )
        changes = apply_ops(repo, ops)
        diff = unified_diff(changes)
        assert "+  await page.waitForLoadState('networkidle');" in diff
        assert "a/tests/flaky-race.spec.ts" in diff and "b/tests/flaky-race.spec.ts" in diff
        assert "waitForLoadState" in spec.read_text()

    def test_missing_anchor_raises(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "x.ts").write_text("nothing relevant here\n")
        op = PatchOp(file="x.ts", op="replace", anchor="no such line", old="a", new="b")
        with pytest.raises(StrategyError, match="anchor not found"):
            apply_ops(repo, [op])

    def test_missing_file_raises(self, tmp_path):
        op = PatchOp(file="ghost.ts", op="replace", anchor="x", old="x", new="y")
        with pytest.raises(StrategyError, match="does not exist"):
            apply_ops(tmp_path, [op])

    def test_patchop_serialization_roundtrip(self):
        op = PatchOp(file="t.ts", op="insert_before", anchor="  await x;", new="  await y;")
        assert PatchOp.from_dict(op.to_dict()) == op


class TestSelection:
    def test_recommended_strategy_first(self, selector_trace):
        diag = {"cause": "fragile_selector", "recommended_strategy": "testid_selector"}
        names = [s.name for s in candidate_strategies(diag, selector_trace)]
        assert names[0] == "testid_selector"

    def test_no_candidates_for_infra(self):
        assert candidate_strategies({"cause": "infra", "recommended_strategy": None}, None) == []
