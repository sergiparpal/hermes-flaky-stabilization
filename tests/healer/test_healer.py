"""Healer orchestrator tests.

Fast paths use FakeSandbox; the end-to-end test really reproduces and heals
flaky-selector.spec.ts through the auto-selected backend (docker when the
daemon is reachable, else the subprocess fallback — plan Phase 3 acceptance).
"""

import json

import pytest
from conftest import FIXTURES, TOY_APP, FakeSandbox, local_browsers_available, make_run_report
from flaky_healer.healer import heal
from flaky_healer.sandbox.base import create_sandbox, docker_available
from flaky_healer.trace import parse_trace

TRACES = FIXTURES / "traces"

e2e_runnable = pytest.mark.skipif(
    not (docker_available() or local_browsers_available()),
    reason="needs docker or local node+browsers to actually run Playwright",
)


@pytest.fixture
def selector_trace():
    return parse_trace(TRACES / "selector.trace.zip")


class TestHealWithFakeSandbox:
    def test_happy_path_reproduce_then_stable(self, tmp_path, selector_trace):
        sandbox = FakeSandbox(
            scripted=[
                make_run_report(5, failures=2),  # reproduce: flaky
                make_run_report(10, failures=0),  # burn-in: all green
            ]
        )
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-selector.spec.ts",
            trace_summary=selector_trace,
            sandbox=sandbox,
        )
        assert report.error is None
        assert report.diagnosis["cause"] == "fragile_selector"
        assert report.strategy == "testid_selector"
        assert report.reproduced is True
        assert report.stable is True
        assert ".getByTestId('submit')" in report.diff
        assert report.llm_calls == 0
        # reproduce ran unpatched on the original; burn-in patched its own copy
        # in place via the prepare hook (no separate pre-copied scratch dir)
        assert sandbox.calls[0]["repo_dir"] == str(TOY_APP)
        assert sandbox.calls[0]["repeats"] == 5
        assert sandbox.calls[0]["prepared"] is False
        assert sandbox.calls[1]["repo_dir"] == str(TOY_APP)
        assert sandbox.calls[1]["repeats"] == 10
        assert sandbox.calls[1]["prepared"] is True
        json.dumps(report.to_dict())

    def test_unreproducible_flake_continues_with_note(self, selector_trace):
        sandbox = FakeSandbox(
            scripted=[
                make_run_report(5, failures=0),  # reproduce: never fails
                make_run_report(10, failures=0),
            ]
        )
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-selector.spec.ts",
            trace_summary=selector_trace,
            sandbox=sandbox,
        )
        assert report.reproduced is False
        assert "did not reproduce" in report.confidence_note
        assert report.stable is True

    def test_failed_burnin_reports_unstable(self, selector_trace):
        sandbox = FakeSandbox(
            scripted=[
                make_run_report(5, failures=3),
                make_run_report(10, failures=1),  # burn-in: one red run
            ]
        )
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-selector.spec.ts",
            trace_summary=selector_trace,
            sandbox=sandbox,
        )
        assert report.stable is False
        assert report.burnin["failed"] == 1

    def test_missing_test_file_is_clean_error(self, selector_trace):
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/ghost.spec.ts",
            trace_summary=selector_trace,
            sandbox=FakeSandbox(),
        )
        assert "not found" in report.error
        assert report.stable is False

    def test_undiagnosable_cause_yields_no_strategy_error(self):
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/stable.spec.ts",
            diagnosis={"cause": "infra", "triage_label": "infra", "recommended_strategy": None},
            sandbox=FakeSandbox(),
        )
        assert "no fix strategy applies" in report.error

    def test_burnin_env_override(self, monkeypatch, selector_trace):
        monkeypatch.setenv("FLAKY_HEALER_BURNIN", "2:3")
        sandbox = FakeSandbox()
        heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-selector.spec.ts",
            trace_summary=selector_trace,
            sandbox=sandbox,
        )
        assert [c["repeats"] for c in sandbox.calls] == [2, 3]

    def test_strategy_override_is_honored(self, selector_trace):
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-selector.spec.ts",
            trace_summary=selector_trace,
            strategy_override="bump_timeout",
            sandbox=FakeSandbox(),
        )
        assert report.strategy == "bump_timeout"
        assert "timeout: 6000" in report.diff  # 2000 * 3


@e2e_runnable
class TestHealEndToEnd:
    def test_heals_flaky_selector_for_real(self, browser_env, selector_trace):
        sandbox = create_sandbox()  # docker when reachable, else subprocess
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-selector.spec.ts",
            trace_summary=selector_trace,
            sandbox=sandbox,
        )
        assert report.error is None, report.to_dict()
        assert report.strategy == "testid_selector"
        assert ".getByTestId('submit')" in report.diff
        assert report.isolation in ("docker", "subprocess")
        assert report.stable is True, (
            "post-patch burn-in must be N/N green: " + json.dumps(report.burnin)
        )
        assert report.burnin["runs"] == 10
        assert report.repro["runs"] == 5

    def test_heals_flaky_race_for_real(self, browser_env):
        race_trace = parse_trace(TRACES / "race.trace.zip")
        report = heal(
            repo_dir=TOY_APP,
            test_id="tests/flaky-race.spec.ts",
            trace_summary=race_trace,
            sandbox=create_sandbox(),
        )
        assert report.error is None, report.to_dict()
        assert report.strategy == "await_state"
        assert "waitForLoadState('networkidle')" in report.diff
        assert report.stable is True, (
            "post-patch burn-in must be N/N green: " + json.dumps(report.burnin)
        )
