"""Failure signatures (plan §4.6).

signature = sha256 over the normalized tuple (framework, error_class,
failing_action, selector_shape, message_shape); shapes strip ids/hashes/
numbers so `#btn-1f9c` and `#btn-59c2` produce the same signature. The
relaxed key drops selector_shape and message_shape (plan §4.6 matcher).
"""

from __future__ import annotations

import hashlib
import json

from ..shapes import message_shape, selector_shape
from ..trace import TraceSummary

PART_KEYS = ("framework", "error_class", "failing_action", "selector_shape", "message_shape")


def signature_parts(
    *,
    framework: str = "playwright",
    error_class: str | None,
    failing_action: str | None,
    selector: str | None,
    message: str | None,
) -> dict:
    return {
        "framework": framework,
        "error_class": error_class or "",
        "failing_action": failing_action or "",
        "selector_shape": selector_shape(selector),
        "message_shape": message_shape(message),
    }


def parts_from_trace(trace: TraceSummary) -> dict:
    return signature_parts(
        framework=trace.framework,
        error_class=trace.error_name,
        failing_action=trace.failing_action,
        selector=trace.selector,
        message=trace.error_message or trace.test_error_message,
    )


def parts_from_diagnosis(diagnosis: dict) -> dict:
    """Weaker fallback when no trace is available (log-only heals)."""
    diagnosis = diagnosis or {}
    return signature_parts(
        error_class=diagnosis.get("error_class"),
        failing_action=diagnosis.get("failing_action"),
        selector=diagnosis.get("selector"),
        message=diagnosis.get("evidence"),
    )


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def signature_hash(parts: dict) -> str:
    return _digest({k: parts.get(k, "") for k in PART_KEYS})


def relaxed_key(parts: dict) -> str:
    return _digest({k: parts.get(k, "") for k in ("framework", "error_class", "failing_action")})
