"""Diagnosis tests: the three flaky fixtures classify via heuristics alone
(no LLM consumed); one ambiguous synthetic case exercises the stub path."""

import pytest
from conftest import FIXTURES
from flaky_healer.diagnose import (
    CAUSES,
    DIAGNOSIS_SCHEMA,
    TRIAGE_FOR_CAUSE,
    diagnose,
    heuristic_diagnosis,
)
from flaky_healer.trace import parse_trace

TRACES = FIXTURES / "traces"


class CountingStub:
    """Injectable complete_structured double that records calls."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    def __call__(self, *, instructions, input, schema=None):
        self.calls.append({"instructions": instructions, "input": input, "schema": schema})
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("fixture", "cause", "strategy"),
    [
        ("selector", "fragile_selector", "testid_selector"),
        ("race", "race_condition", "await_state"),
        ("timeout", "timeout", "bump_timeout"),
    ],
)
def test_flaky_fixtures_classified_by_heuristics_alone(fixture, cause, strategy):
    stub = CountingStub(response={"cause": "unknown"})
    trace = parse_trace(TRACES / f"{fixture}.trace.zip")
    diag = diagnose(trace=trace, complete_structured=stub)
    assert diag["cause"] == cause
    assert diag["recommended_strategy"] == strategy
    assert diag["confidence"] >= 0.7
    assert diag["diagnosis_stage"] == "heuristic"
    assert diag["llm_used"] is False
    assert stub.calls == [], "high-confidence heuristics must not consume the LLM"
    assert diag["triage_label"] == TRIAGE_FOR_CAUSE[cause]
    assert diag["evidence"]


def test_ambiguous_case_escalates_to_llm_stub():
    stub = CountingStub(
        response={
            "cause": "data",
            "triage_label": "data",
            "evidence": "checksum mismatch indicates corrupted seed data",
            "confidence": 0.9,
        }
    )
    trace = parse_trace(TRACES / "ambiguous-synthetic.trace.zip")
    assert heuristic_diagnosis(trace)["confidence"] < 0.7
    diag = diagnose(trace=trace, complete_structured=stub)
    assert len(stub.calls) == 1, "low confidence must consult the LLM exactly once"
    assert stub.calls[0]["schema"] == DIAGNOSIS_SCHEMA
    assert diag["cause"] == "data"
    assert diag["diagnosis_stage"] == "llm"
    assert diag["llm_used"] is True
    assert diag["confidence"] == 0.9


def test_llm_failure_falls_back_to_heuristics():
    stub = CountingStub(error=RuntimeError("provider exploded"))
    trace = parse_trace(TRACES / "ambiguous-synthetic.trace.zip")
    diag = diagnose(trace=trace, complete_structured=stub)
    assert diag["cause"] == "unknown"
    assert diag["diagnosis_stage"] == "heuristic"
    assert "provider exploded" in diag["llm_error"]


def test_llm_garbage_is_rejected():
    stub = CountingStub(response={"cause": "made_up_cause", "confidence": 5})
    trace = parse_trace(TRACES / "ambiguous-synthetic.trace.zip")
    diag = diagnose(trace=trace, complete_structured=stub)
    assert diag["cause"] in CAUSES
    assert diag["cause"] == "unknown", "invalid LLM cause must fall back to heuristics"


def test_no_llm_callable_keeps_low_confidence_heuristic():
    trace = parse_trace(TRACES / "ambiguous-synthetic.trace.zip")
    diag = diagnose(trace=trace, complete_structured=None)
    assert diag["cause"] == "unknown"
    assert diag["llm_used"] is False


def test_log_only_environment_classification():
    log = "connect ECONNREFUSED 10.0.0.7:5432 while seeding the database"
    diag = diagnose(trace=None, log_excerpt=log)
    assert diag["cause"] == "environment"
    assert diag["triage_label"] == "environment"


def test_log_only_timeout_below_threshold():
    log = "TimeoutError: locator.click: Timeout 2000ms exceeded.\n  - waiting for locator('#x')"
    diag = heuristic_diagnosis(None, log)
    assert diag["cause"] == "timeout"
    assert diag["selector"] == "#x"
    assert diag["confidence"] < 0.7, "log-only evidence is weaker than a parsed trace"


def test_infra_classification_from_trace_text():
    log = "Error: browserType.launch: Target page, context or browser has been closed"
    diag = diagnose(trace=None, log_excerpt=log)
    assert diag["cause"] == "infra"
    assert diag["recommended_strategy"] is None


def test_diagnosis_schema_matches_plan_taxonomy():
    assert set(DIAGNOSIS_SCHEMA["properties"]["cause"]["enum"]) == set(CAUSES)
    assert set(DIAGNOSIS_SCHEMA["properties"]["triage_label"]["enum"]) == set(
        TRIAGE_FOR_CAUSE.values()
    ) | {"broken_test"}
    assert DIAGNOSIS_SCHEMA["required"] == ["cause", "triage_label", "evidence", "confidence"]
