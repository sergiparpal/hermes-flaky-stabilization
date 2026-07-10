"""Two-stage flakiness diagnosis (plan §4.2 / Phase 2.2).

Stage 1: deterministic heuristics over the parsed trace + filtered log.
Stage 2: an injected ``complete_structured`` callable (the host's ctx.llm),
consulted ONLY when stage-1 confidence < 0.7. This module never imports
Hermes; the callable is dependency-injected so tests run with a stub.
"""

from __future__ import annotations

import re

from .trace import TraceSummary

CAUSES = (
    "timeout",
    "fragile_selector",
    "race_condition",
    "environment",
    "data",
    "infra",
    "unknown",
)

# Mapping into the hermes-ci-triage taxonomy for composability (plan §4.2).
TRIAGE_FOR_CAUSE = {
    "timeout": "timeout",
    "fragile_selector": "flaky",
    "race_condition": "flaky",
    "environment": "environment",
    "data": "data",
    "infra": "infra",
    "unknown": "broken_test",
}

STRATEGY_FOR_CAUSE = {
    "timeout": "bump_timeout",
    "fragile_selector": "testid_selector",
    "race_condition": "await_state",
}

CONFIDENCE_THRESHOLD = 0.7

# Per-rule confidence for the heuristic (stage-1) diagnosis. Named so the ladder
# of thresholds can be read and tuned in one place instead of as bare floats
# scattered through the rule cascade. Ordered most- to least-certain.
_CONF_FRAGILE_SELECTOR = 0.85    # trace proves a same-shaped data-testid target
_CONF_ENVIRONMENT = 0.8          # network-level failure marker in the output
_CONF_RACE_CONDITION = 0.8       # timed out with requests still in flight
_CONF_INFRA = 0.75               # browser/runner infrastructure failure
_CONF_TIMEOUT = 0.75             # element resolved but never reached its state
_CONF_TIMEOUT_LOG_ONLY = 0.6     # timeout marker in a CI log, no trace to confirm
_CONF_DATA = 0.5                 # assertion failed without timing out
_CONF_UNKNOWN = 0.3              # no deterministic rule matched

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "cause": {"type": "string", "enum": list(CAUSES)},
        "triage_label": {
            "type": "string",
            "enum": ["broken_test", "environment", "data", "timeout", "flaky", "infra"],
        },
        "evidence": {"type": "string"},
        "failing_action": {"type": ["string", "null"]},
        "selector": {"type": ["string", "null"]},
        "recommended_strategy": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["cause", "triage_label", "evidence", "confidence"],
}

_ENVIRONMENT_RE = re.compile(
    r"(ECONNREFUSED|ENOTFOUND|EHOSTUNREACH|EAI_AGAIN|ERR_CONNECTION_REFUSED"
    r"|ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED)",
    re.IGNORECASE,
)
_INFRA_RE = re.compile(
    r"(target page, context or browser has been closed|browser has been closed"
    r"|browserType\.launch|browser crash|crashed|SIGKILL|SIGSEGV|out of memory)",
    re.IGNORECASE,
)
_LOG_TIMEOUT_RE = re.compile(r"Timeout \d+ms exceeded", re.IGNORECASE)
_LOG_SELECTOR_RE = re.compile(r"waiting for locator\('([^']+)'\)")


def _make_diagnosis(cause, confidence, evidence, failing_action=None, selector=None) -> dict:
    return {
        "cause": cause,
        "triage_label": TRIAGE_FOR_CAUSE[cause],
        "evidence": evidence,
        "failing_action": failing_action,
        "selector": selector,
        "recommended_strategy": STRATEGY_FOR_CAUSE.get(cause),
        "confidence": confidence,
    }


def heuristic_diagnosis(trace: TraceSummary | None, log_excerpt: str | None = None) -> dict:
    """Stage 1: ordered deterministic rules. Always returns a diagnosis dict."""
    text_parts = []
    if trace is not None:
        text_parts += [trace.error_message or "", trace.test_error_message or ""]
        text_parts += trace.call_log
    if log_excerpt:
        text_parts.append(log_excerpt)
    text = "\n".join(text_parts)

    action = trace.failing_action if trace else None
    selector = trace.selector if trace else None
    if selector is None and log_excerpt:
        m = _LOG_SELECTOR_RE.search(log_excerpt)
        if m:
            selector = m.group(1)

    m = _ENVIRONMENT_RE.search(text)
    if m:
        return _make_diagnosis(
            "environment",
            _CONF_ENVIRONMENT,
            f"network-level failure marker '{m.group(1)}' in the failure output",
            action,
            selector,
        )
    m = _INFRA_RE.search(text)
    if m:
        return _make_diagnosis(
            "infra",
            _CONF_INFRA,
            f"browser/runner infrastructure failure: '{m.group(1)}'",
            action,
            selector,
        )

    if trace is not None and trace.timed_out:
        if selector and not trace.selector_resolved and trace.testid_on_target:
            return _make_diagnosis(
                "fragile_selector",
                _CONF_FRAGILE_SELECTOR,
                f"{action} timed out waiting for {selector!r} which never attached, but the "
                f"DOM snapshot shows a same-shaped element carrying "
                f"data-testid={trace.testid_on_target!r}",
                action,
                selector,
            )
        if trace.pending_requests:
            urls = ", ".join(r.url for r in trace.pending_requests[:3])
            return _make_diagnosis(
                "race_condition",
                _CONF_RACE_CONDITION,
                f"{action} timed out while network requests were still unsettled ({urls}); "
                "the assertion ran before the data it depends on arrived",
                action,
                selector,
            )
        return _make_diagnosis(
            "timeout",
            _CONF_TIMEOUT,
            f"{action} on {selector!r} timed out after {trace.timeout_ms}ms; the element "
            "resolved but did not reach the expected state in time and no network requests "
            "were pending",
            action,
            selector,
        )

    if trace is None and log_excerpt and _LOG_TIMEOUT_RE.search(log_excerpt):
        return _make_diagnosis(
            "timeout",
            _CONF_TIMEOUT_LOG_ONLY,
            "timeout marker found in CI log (no trace available for deeper analysis)",
            action,
            selector,
        )

    if trace is not None and trace.error_name and "expect" in (action or ""):
        if not trace.timed_out:
            return _make_diagnosis(
                "data",
                _CONF_DATA,
                f"assertion {action} failed without timing out — received value likely "
                "differs from expectation (possible test-data drift)",
                action,
                selector,
            )

    return _make_diagnosis(
        "unknown",
        _CONF_UNKNOWN,
        "no deterministic rule matched the failure evidence",
        action,
        selector,
    )


def _evidence_bundle(trace: TraceSummary | None, log_excerpt: str | None) -> dict:
    bundle: dict = {"framework": "playwright"}
    if trace is not None:
        bundle.update(trace.brief())
    if log_excerpt:
        bundle["ci_log_excerpt"] = log_excerpt[:8192]
    return bundle


_LLM_INSTRUCTIONS = (
    "You are a senior QA engineer triaging a failed Playwright test. Based on the structured "
    "evidence (parsed trace summary and/or filtered CI log), classify the root cause as one of: "
    "timeout, fragile_selector, race_condition, environment, data, infra, unknown. Map it to a "
    "triage_label (broken_test|environment|data|timeout|flaky|infra), cite the decisive evidence "
    "in one or two sentences, and recommend a fix strategy among bump_timeout, testid_selector, "
    "await_state, or null when no automated strategy applies. Be conservative with confidence."
)


def _coerce_str_or_none(value, fallback):
    """LLM fields are untrusted: keep a string, else fall back (never a bare int)."""
    return value if isinstance(value, str) else fallback


def _sanitize_llm_diagnosis(raw: dict, fallback: dict) -> dict:
    if not isinstance(raw, dict):
        return fallback
    cause = raw.get("cause")
    if cause not in CAUSES:
        return fallback
    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    out = _make_diagnosis(
        cause,
        min(1.0, max(0.0, confidence)),
        str(raw.get("evidence") or "model-provided diagnosis"),
        _coerce_str_or_none(raw.get("failing_action"), fallback.get("failing_action")),
        _coerce_str_or_none(raw.get("selector"), fallback.get("selector")),
    )
    strategy = raw.get("recommended_strategy")
    if strategy in STRATEGY_FOR_CAUSE.values():
        out["recommended_strategy"] = strategy
    triage = raw.get("triage_label")
    if triage in set(TRIAGE_FOR_CAUSE.values()):
        out["triage_label"] = triage
    return out


def diagnose(
    trace: TraceSummary | None = None,
    log_excerpt: str | None = None,
    complete_structured=None,
    threshold: float = CONFIDENCE_THRESHOLD,
    screenshots: list | None = None,
) -> dict:
    """Stage-1 heuristics, escalating to the injected LLM below `threshold`.

    ``screenshots`` is accepted (and forwarded in the evidence bundle) to keep
    the interface ready for multimodal diagnosis; the MVP sends none.
    """
    diag = heuristic_diagnosis(trace, log_excerpt)
    diag["diagnosis_stage"] = "heuristic"
    diag["llm_used"] = False
    if diag["confidence"] >= threshold or complete_structured is None:
        return diag

    bundle = _evidence_bundle(trace, log_excerpt)
    if screenshots:
        bundle["screenshots"] = screenshots
    try:
        raw = complete_structured(
            instructions=_LLM_INSTRUCTIONS,
            input=bundle,
            schema=DIAGNOSIS_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 — LLM failure must not kill diagnosis
        diag["llm_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return diag

    merged = _sanitize_llm_diagnosis(raw, diag)
    if merged is diag:  # payload rejected — keep the heuristic result and its provenance
        diag["llm_used"] = False
        diag["llm_rejected"] = True
        return diag
    merged["diagnosis_stage"] = "llm"
    merged["llm_used"] = True
    return merged
