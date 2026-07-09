"""Contract-parity for the Phase-3 tools vs their legacy plugins.

Same inputs, JSON-deep-equal outputs — with the LLM faked identically on both
sides where one is involved. Legacy packages are spec-loaded under private
module names so the alias conftests (which map the legacy import names onto
the unified stages for the ported suites) can never leak into these tests.
Skips cleanly when the sibling checkouts are absent.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SIBLINGS = Path(__file__).resolve().parent.parent.parent
LEGACY_MV = SIBLINGS / "hermes-masking-validator"
LEGACY_BR = SIBLINGS / "hermes-bug-report-improver"
LEGACY_CT = SIBLINGS / "hermes-ci-triage"

pytestmark = pytest.mark.skipif(
    not (LEGACY_MV.is_dir() and LEGACY_BR.is_dir() and LEGACY_CT.is_dir()),
    reason="legacy sibling checkouts not available",
)


def _spec_load(name: str, package_dir: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- validate_no_pii ---------------------------------------------------------


def test_validate_no_pii_parity(tmp_path):
    legacy = _spec_load("legacy_masking_validator", LEGACY_MV)
    from hermes_flaky_stabilization import pii as unified

    evidence = tmp_path / "evidence"
    (evidence / "sub").mkdir(parents=True)
    (evidence / "a.log").write_text(
        "user mail: alice@example.com\ncard: 4111 1111 1111 1111\n", encoding="utf-8"
    )
    (evidence / "sub" / "b.txt").write_text(
        "iban ES9121000418450200051332 and dni 12345678Z\n", encoding="utf-8"
    )
    (evidence / "clean.txt").write_text("nothing sensitive here\n", encoding="utf-8")

    for params in (
        {"target": str(evidence)},
        {"target": str(evidence / "a.log")},
        {"target": str(evidence), "types": ["email"]},
        {"target": str(evidence), "max_files": 1},
        {"target": str(tmp_path / "missing")},
        {"target": ""},
    ):
        legacy_out = json.loads(legacy._handle_validate_no_pii(dict(params)))
        unified_out = json.loads(unified._handle_validate_no_pii(dict(params)))
        assert unified_out == legacy_out, params


# --- improve_bug_report --------------------------------------------------------


class _Result:
    def __init__(self, parsed):
        self.parsed = parsed
        self.text = json.dumps(parsed)
        self.content_type = "json"


class _QueueLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def complete_structured(self, **kwargs):
        return _Result(self._responses.pop(0))


class _Ctx:
    def __init__(self, responses):
        self.llm = _QueueLLM(responses)


_REPORT = {
    "title": "Checkout button unresponsive on Safari",
    "summary": "Clicking checkout does nothing on Safari 17.",
    "reproduction_steps": ["Open cart", "Click checkout"],
    "expected_behavior": "Order review page opens.",
    "actual_behavior": "Nothing happens; console shows TypeError.",
    "severity": "high",
    "severity_rationale": "Blocks purchases for Safari users.",
    "missing_evidence": ["Safari version", "console log"],
}


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_improve_bug_report_parity(fmt):
    legacy_handler = _spec_load(
        "legacy_bug_report_improver", LEGACY_BR / "hermes_bug_report_improver"
    )
    from hermes_flaky_stabilization import bugreport as unified

    args = {"raw_text": "checkout broken on safari, does nothing", "format": fmt}
    legacy_out = legacy_handler.handler.make_handler(_Ctx([dict(_REPORT)]))(dict(args))
    unified_out = unified.handler.make_handler(_Ctx([dict(_REPORT)]))(dict(args))
    assert unified_out == legacy_out


# --- triage_pipeline_failure (heuristic path, no LLM) --------------------------


def test_triage_parity_heuristic(tmp_path, profile_env):
    legacy = _spec_load("legacy_ci_triage", LEGACY_CT)
    from hermes_flaky_stabilization.triage import handlers as unified_handlers

    log = tmp_path / "fail.log"
    log.write_text(
        "collecting tests\n"
        "E   ModuleNotFoundError: No module named 'requests'\n"
        "FAILED tests/test_api.py::test_fetch - ModuleNotFoundError\n",
        encoding="utf-8",
    )
    args = {"log_url_or_path": str(log), "project": "parity"}
    legacy_out = json.loads(
        legacy.handlers.triage_pipeline_failure(
            dict(args), llm=None, hermes_home=str(profile_env)
        )
    )
    unified_out = json.loads(
        unified_handlers.triage_pipeline_failure(
            dict(args), llm=None, hermes_home=str(profile_env)
        )
    )
    assert unified_out == legacy_out

    # Second run: both sides must see their own prior (separate stores, same answer).
    legacy_out2 = json.loads(
        legacy.handlers.triage_pipeline_failure(
            dict(args), llm=None, hermes_home=str(profile_env)
        )
    )
    unified_out2 = json.loads(
        unified_handlers.triage_pipeline_failure(
            dict(args), llm=None, hermes_home=str(profile_env)
        )
    )
    assert unified_out2 == legacy_out2
    assert unified_out2["prior_seen"] is True
