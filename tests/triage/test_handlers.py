"""handlers: end-to-end pipeline with a mocked llm."""

from __future__ import annotations

import json

import pytest
from hermes_plugins.hermes_ci_triage import classifier, handlers

# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

class _FakeStructured:
    def __init__(self, parsed, content_type="json", text=""):
        self.parsed = parsed
        self.content_type = content_type
        self.text = text


class FakeLlm:
    """Stand-in for ctx.llm. Records calls; returns a canned structured result."""

    def __init__(self, parsed=None, content_type="json", raise_exc=None):
        self._parsed = parsed
        self._ct = content_type
        self._raise = raise_exc
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return _FakeStructured(self._parsed, self._ct)


# Each fixture is crafted so the rule-based heuristic resolves it to its label.
CATEGORY_FIXTURES = {
    "broken_test": "AssertionError: expected 5 but got 4\nFAILED tests/test_math.py::test_add\n",
    "environment": "ModuleNotFoundError: No module named 'requests'\npip install failed\n",
    "data": "json.decoder.JSONDecodeError: Expecting value: line 1 column 1\nfixture load failed\n",
    "timeout": "step timed out after 600s\nTimeoutError: deadline exceeded\n",
    "flaky": "test failed but passed on retry (flaky)\nintermittent failure detected\n",
    "infra": "No space left on device\nrunner disconnected mid-job\n",
}


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------
# Heuristic path (llm unavailable) — one fixture per taxonomy category
# --------------------------------------------------------------------------

@pytest.mark.parametrize("expected,log_text", list(CATEGORY_FIXTURES.items()))
def test_heuristic_classifies_each_category(tmp_path, tmp_hermes_home, expected, log_text):
    path = _write(tmp_path, f"{expected}.log", log_text)
    out = handlers.triage_pipeline_failure({"log_url_or_path": path, "project": "p"}, llm=None)
    data = json.loads(out)
    assert data["success"] is True
    assert data["category"] == expected
    assert data["classification_method"] == "heuristic"
    assert isinstance(out, str)


# --------------------------------------------------------------------------
# LLM path takes precedence and passes the schema through
# --------------------------------------------------------------------------

def test_llm_path_used_when_valid(tmp_path, tmp_hermes_home):
    # A broken_test-looking log, but the (mocked) llm says 'infra' — llm wins.
    path = _write(tmp_path, "x.log", "AssertionError: boom\nFAILED test_x\n")
    llm = FakeLlm(parsed={"category": "infra", "confidence": 0.92,
                          "summary": "runner died", "evidence": ["x"],
                          "suggested_action": "retry"})
    out = handlers.triage_pipeline_failure({"log_url_or_path": path}, llm=llm)
    data = json.loads(out)
    assert data["category"] == "infra"
    assert data["classification_method"] == "llm"
    assert data["confidence"] == 0.92
    # The fixed taxonomy schema was passed to the structured call.
    assert llm.calls and llm.calls[0]["json_schema"] is classifier.CLASSIFICATION_SCHEMA


def test_invalid_llm_output_falls_back_to_heuristic(tmp_path, tmp_hermes_home):
    path = _write(tmp_path, "x.log", "ModuleNotFoundError: No module named 'x'\n")
    # content_type 'text' (e.g. the model refused / returned prose) → fallback.
    llm = FakeLlm(parsed=None, content_type="text")
    out = handlers.triage_pipeline_failure({"log_url_or_path": path}, llm=llm)
    data = json.loads(out)
    assert data["success"] is True
    assert data["category"] == "environment"
    assert data["classification_method"] == "heuristic"


def test_llm_raising_falls_back(tmp_path, tmp_hermes_home):
    path = _write(tmp_path, "x.log", "No space left on device\n")
    llm = FakeLlm(raise_exc=ValueError("schema mismatch"))
    out = handlers.triage_pipeline_failure({"log_url_or_path": path}, llm=llm)
    data = json.loads(out)
    assert data["success"] is True
    assert data["category"] == "infra"


# --------------------------------------------------------------------------
# Error contract
# --------------------------------------------------------------------------

def test_missing_local_file_structured_error(tmp_path, tmp_hermes_home):
    out = handlers.triage_pipeline_failure(
        {"log_url_or_path": str(tmp_path / "absent.log")}, llm=None)
    data = json.loads(out)
    assert data["success"] is False
    assert "error" in data and "remediation" in data


def test_missing_argument_structured_error(tmp_hermes_home):
    data = json.loads(handlers.triage_pipeline_failure({}, llm=None))
    assert data["success"] is False
    assert "log_url_or_path" in data["error"]


# --------------------------------------------------------------------------
# Pattern learning + guarded enrichment
# --------------------------------------------------------------------------

def test_prior_seen_after_second_run(tmp_path, tmp_hermes_home):
    path = _write(tmp_path, "x.log", "AssertionError: boom\nFAILED test_x\n")
    args = {"log_url_or_path": path, "project": "proj"}
    first = json.loads(handlers.triage_pipeline_failure(args, llm=None))
    assert first["prior_seen"] is False
    assert first["prior_occurrences"] == 0
    second = json.loads(handlers.triage_pipeline_failure(args, llm=None))
    assert second["prior_seen"] is True
    assert second["prior_occurrences"] == 1


# Unified-plugin adaptation (plan §2 row 6): enrichment no longer dispatches
# partner tools — it calls the in-package history stage directly, with the
# correct arguments. The failure seam is the direct lookup itself.

def test_enrichment_failure_is_non_fatal(tmp_path, tmp_hermes_home, monkeypatch):
    from hermes_plugins.hermes_ci_triage import enrichment

    path = _write(tmp_path, "x.log", "AssertionError: boom\n")

    def exploding_lookup(query):
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(enrichment, "_history_lookup", exploding_lookup)
    out = handlers.triage_pipeline_failure(
        {"log_url_or_path": path}, llm=None, enable_enrichment=True)
    data = json.loads(out)
    assert data["success"] is True  # enrichment failure did not break triage
    assert "enrichment" not in data


def test_enrichment_used_when_present(tmp_path, tmp_hermes_home, seeded_history):
    path = _write(tmp_path, "x.log", "FAILED test_checkout_flow\n")
    out = handlers.triage_pipeline_failure(
        {"log_url_or_path": path}, llm=None, enable_enrichment=True)
    data = json.loads(out)
    assert data["success"] is True
    assert data.get("enrichment", {}).get("source") == "test_failure_lookup"
    result = data["enrichment"]["result"]
    # Regression for the legacy arg-key bug: the enrichment payload must be
    # real lookup data, never a {"success": false, ...} validation envelope.
    assert result.get("matched_tests", 0) >= 1
    assert "error" not in result
    assert result.get("success") is not False
    names = {f.get("name") for f in result.get("failures", [])}
    assert "test_checkout_flow" in names


def test_low_signal_log_still_succeeds(tmp_path, tmp_hermes_home):
    path = _write(tmp_path, "clean.log", "\n".join(f"step {i} ok" for i in range(50)))
    data = json.loads(handlers.triage_pipeline_failure(
        {"log_url_or_path": path}, llm=None))
    assert data["success"] is True
    assert data["log_stats"]["low_signal"] is True
    assert data["category"] in classifier.TAXONOMY


def test_low_signal_heuristic_is_not_learned(tmp_path, tmp_hermes_home):
    """L7: low-signal heuristic defaults must not pollute the pattern store."""
    path = _write(tmp_path, "clean.log", "\n".join(f"step {i} ok" for i in range(40)))
    args = {"log_url_or_path": path, "project": "lp"}
    first = json.loads(handlers.triage_pipeline_failure(args, llm=None))
    assert first["log_stats"]["low_signal"] is True
    assert first["prior_seen"] is False
    second = json.loads(handlers.triage_pipeline_failure(args, llm=None))
    assert second["prior_seen"] is False  # nothing was recorded the first time


def test_blank_log_does_not_collide_in_store(tmp_path, tmp_hermes_home):
    """L1: an excerpt that normalises to empty is never recorded."""
    path = _write(tmp_path, "blank.log", "   \n\n   \n")
    args = {"log_url_or_path": path, "project": "bp"}
    first = json.loads(handlers.triage_pipeline_failure(args, llm=None))
    assert first["success"] is True
    assert first["prior_seen"] is False
    second = json.loads(handlers.triage_pipeline_failure(args, llm=None))
    assert second["prior_seen"] is False


def test_secrets_redacted_in_output(tmp_path, tmp_hermes_home):
    """M3: secrets in a CI log are not echoed back in the tool result."""
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    log = f"AssertionError: boom\nleaked token={secret}\nFAILED tests/test_x.py\n"
    path = _write(tmp_path, "x.log", log)
    out = handlers.triage_pipeline_failure({"log_url_or_path": path}, llm=None)
    assert secret not in out
    assert "ghp_" not in out
    data = json.loads(out)
    assert data["success"] is True
    assert data["category"] == "broken_test"  # classification still works


def test_secret_excerpt_not_sent_to_llm(tmp_path, tmp_hermes_home):
    """M3: the excerpt handed to the LLM is already redacted."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    path = _write(tmp_path, "x.log", f"AssertionError: boom\naws_key {secret}\n")
    llm = FakeLlm(parsed={"category": "broken_test", "confidence": 0.9,
                          "summary": "ok", "evidence": [], "suggested_action": "x"})
    handlers.triage_pipeline_failure({"log_url_or_path": path}, llm=llm)
    assert secret not in json.dumps(llm.calls[0]["input"])


def test_llm_error_is_logged(tmp_path, tmp_hermes_home, caplog):
    """H1: an LLM failure is logged (not silently swallowed) before fallback."""
    import logging

    path = _write(tmp_path, "x.log", "No space left on device\n")
    llm = FakeLlm(raise_exc=ValueError("boom"))
    with caplog.at_level(logging.WARNING):
        handlers.triage_pipeline_failure({"log_url_or_path": path}, llm=llm)
    assert any("heuristic fallback" in r.getMessage() for r in caplog.records)


def test_non_json_llm_output_logs_warning(tmp_path, tmp_hermes_home, caplog):
    """Regression: content_type != 'json' fell back with NO log line, so a host
    misintegration silently degraded every call to 0.3-confidence heuristics."""
    import logging

    path = _write(tmp_path, "x.log", "ModuleNotFoundError: No module named 'x'\n")
    llm = FakeLlm(parsed=None, content_type="text")
    with caplog.at_level(logging.WARNING):
        data = json.loads(handlers.triage_pipeline_failure(
            {"log_url_or_path": path}, llm=llm))
    assert data["classification_method"] == "heuristic"
    assert any(
        "content_type" in r.getMessage() and "'text'" in r.getMessage()
        for r in caplog.records
    )


def test_out_of_taxonomy_category_logs_warning(tmp_path, tmp_hermes_home, caplog):
    """Regression: an out-of-taxonomy category fell back silently; the warning
    must name the offending category."""
    import logging

    path = _write(tmp_path, "x.log", "No space left on device\n")
    llm = FakeLlm(parsed={"category": "cosmic_rays", "confidence": 0.9,
                          "summary": "x"})
    with caplog.at_level(logging.WARNING):
        data = json.loads(handlers.triage_pipeline_failure(
            {"log_url_or_path": path}, llm=llm))
    assert data["category"] == "infra"
    assert data["classification_method"] == "heuristic"
    assert any("cosmic_rays" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Low-signal tail excerpt: character cap (a line cap alone is no bound)
# --------------------------------------------------------------------------

def test_low_signal_single_line_log_is_char_capped(tmp_path, tmp_hermes_home):
    """Regression: a huge single-line low-signal log (CR progress bars,
    minified output) went whole through redaction, the signature regexes and
    into the LLM call — _tail_excerpt had no character cap."""
    from hermes_plugins.hermes_ci_triage import prefilter

    # One line, ~90k chars, no failure keyword anywhere.
    log = "HEADzz " + "ok " * 30_000 + "TAILzz"
    path = _write(tmp_path, "huge.log", log)
    llm = FakeLlm(parsed={"category": "infra", "confidence": 0.6,
                          "summary": "s", "evidence": [],
                          "suggested_action": "a"})
    data = json.loads(handlers.triage_pipeline_failure(
        {"log_url_or_path": path}, llm=llm))
    assert data["success"] is True
    assert data["log_stats"]["low_signal"] is True
    assert data["log_stats"]["truncated"] is True
    # The excerpt handed to the LLM is capped, tail-kept, and carries the note.
    excerpt_block = llm.calls[0]["input"][-1]["text"]
    assert len(excerpt_block) <= (
        prefilter.DEFAULT_CHAR_CAP + len(prefilter.TRUNCATION_NOTE) + 100
    )
    assert prefilter.TRUNCATION_NOTE in excerpt_block
    assert "TAILzz" in excerpt_block   # errors cluster at the end — keep the tail
    assert "HEADzz" not in excerpt_block


def test_tail_excerpt_small_log_unchanged():
    """Small low-signal logs are untouched — no note, no truncation."""
    from hermes_plugins.hermes_ci_triage import prefilter

    raw = "\n".join(f"step {i} ok" for i in range(10))
    out = handlers._tail_excerpt(raw)
    assert out == raw
    assert prefilter.TRUNCATION_NOTE not in out


# --------------------------------------------------------------------------
# Enrichment query seeding must skip prefilter-injected marker lines
# --------------------------------------------------------------------------

def test_top_signal_line_skips_prefilter_markers():
    """Regression: the truncation note contains the word 'failure', so it is a
    'failure line' — and it is the excerpt's first line, so every truncated
    excerpt used the note itself as the history query."""
    from hermes_plugins.hermes_ci_triage import enrichment, prefilter

    # Premise of the bug: the note itself matches the failure-line detector.
    assert prefilter.is_failure_line(prefilter.TRUNCATION_NOTE)
    excerpt = (
        prefilter.TRUNCATION_NOTE + "\n"
        "        ... (42 lines omitted) ...\n"
        "        ⟪ previous line repeated 7 more times ⟫\n"
        "AssertionError: boom in test_checkout\n"
    )
    assert enrichment.top_signal_line(excerpt) == "AssertionError: boom in test_checkout"


def test_top_signal_line_marker_only_excerpt_yields_no_query():
    from hermes_plugins.hermes_ci_triage import enrichment, prefilter

    excerpt = prefilter.TRUNCATION_NOTE + "\n        ... (5 lines omitted) ...\n"
    assert enrichment.top_signal_line(excerpt) == ""
    assert enrichment.enrich(excerpt) is None  # empty query → no lookup at all


def test_enrichment_works_on_truncated_excerpt(tmp_path, tmp_hermes_home, seeded_history):
    """End-to-end: a truncated big log still enriches from a real signal line,
    not from the truncation note the prefilter prepends."""
    lines = []
    for i in range(1000):
        lines.append(f"collecting batch {i}")
        lines.append("FAILED test_checkout_flow")
    path = _write(tmp_path, "big.log", "\n".join(lines) + "\n")
    out = handlers.triage_pipeline_failure(
        {"log_url_or_path": path}, llm=None, enable_enrichment=True)
    data = json.loads(out)
    assert data["success"] is True
    assert data["log_stats"]["truncated"] is True
    assert data.get("enrichment", {}).get("source") == "test_failure_lookup"


# --------------------------------------------------------------------------
# fetch_truncated surfaces in log_stats (remote tail-kept fetch only)
# --------------------------------------------------------------------------

def test_fetch_truncated_flag_surfaces_in_log_stats(tmp_hermes_home, monkeypatch):
    monkeypatch.setattr(
        handlers.logfetch, "fetch_with_stats",
        lambda target, **kw: ("AssertionError: boom\n", {"fetch_truncated": True}),
    )
    data = json.loads(handlers.triage_pipeline_failure(
        {"log_url_or_path": "https://logs.example/big"}, llm=None))
    assert data["success"] is True
    assert data["log_stats"]["fetch_truncated"] is True


def test_local_fetch_has_no_truncated_flag(tmp_path, tmp_hermes_home):
    """Local reads hard-fail on oversize instead of truncating; the key is
    omitted so the envelope stays parity-identical with the legacy plugin."""
    path = _write(tmp_path, "x.log", "AssertionError: boom\n")
    data = json.loads(handlers.triage_pipeline_failure(
        {"log_url_or_path": path}, llm=None))
    assert "fetch_truncated" not in data["log_stats"]
