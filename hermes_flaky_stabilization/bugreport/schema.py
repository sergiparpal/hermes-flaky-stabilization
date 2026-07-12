"""Input/output JSON schemas and shared constants for ``improve_bug_report``.

``TOOL_SCHEMA`` is the full tool schema passed to ``ctx.register_tool`` (it has
top-level ``name``/``description``/``parameters`` keys, as Hermes expects).
``BUG_REPORT_OUTPUT_SCHEMA`` is the output contract enforced by
``ctx.llm.complete_structured(json_schema=...)``.
"""

from __future__ import annotations

TOOL_NAME = "improve_bug_report"
TOOLSET = "qa"

TOOL_DESCRIPTION = (
    "Takes a poorly written or incomplete bug report and returns a structured "
    "version with title, reproduction steps, expected and actual behavior, "
    "suggested severity, and a list of missing evidence. Does not invent "
    "missing facts."
)

ALLOWED_FORMATS = ("markdown", "json")
DEFAULT_FORMAT = "markdown"
# Severity rubric (fixed in v0.1.0) — single source of truth for both the
# allowed levels and their definitions (referenced by the prompt and the README).
SEVERITY_RUBRIC = {
    "critical": "data loss, security breach, full service outage, blocks all users.",
    "high": "blocks a major workflow, affects many users, no workaround available.",
    "medium": "degraded experience, workaround exists, affects a subset of users.",
    "low": "cosmetic, minor, edge case.",
    "unknown": "insufficient signal in the input to assign a level.",
}
SEVERITY_LEVELS = tuple(SEVERITY_RUBRIC)
MAX_RAW_TEXT_BYTES = 16 * 1024  # 16 KB cap on raw_text.
MAX_CONTEXT_BYTES = 4 * 1024  # 4 KB cap on the optional context string.
MAX_TITLE_CHARS = 80  # Title length cap; enforced by the handler when coercing.
MAX_FIELD_CHARS = 4_000
MAX_LIST_ITEMS = 50
MAX_LIST_ITEM_CHARS = 1_000

# --- Input schema (what the agent passes to the tool) --------------------------
# Wrapped as a full tool schema: name/description/parameters.
TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "raw_text": {
                "type": "string",
                "description": "The original, unstructured bug report text.",
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional additional environment or context info (versions, "
                    "OS, browser, build number, etc.)."
                ),
                "default": "",
            },
            "format": {
                "type": "string",
                "enum": list(ALLOWED_FORMATS),
                "default": DEFAULT_FORMAT,
                "description": (
                    "Output format. 'markdown' for human consumption, 'json' for "
                    "downstream tooling."
                ),
            },
        },
        "required": ["raw_text"],
    },
}

# --- Output schema (what the model must return) --------------------------------
# Enforced by ctx.llm.complete_structured(json_schema=...).
BUG_REPORT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": f"Concise bug title, at most {MAX_TITLE_CHARS} characters.",
        },
        "summary": {"type": "string", "maxLength": MAX_FIELD_CHARS,
                    "description": "One or two sentences."},
        "reproduction_steps": {
            "type": "array",
            "items": {"type": "string", "maxLength": MAX_LIST_ITEM_CHARS},
            "maxItems": MAX_LIST_ITEMS,
            "description": "Ordered steps to reproduce. Empty array if none given.",
        },
        "expected_behavior": {"type": "string", "maxLength": MAX_FIELD_CHARS},
        "actual_behavior": {"type": "string", "maxLength": MAX_FIELD_CHARS},
        "severity": {"type": "string", "enum": list(SEVERITY_LEVELS)},
        "severity_rationale": {
            "type": "string",
            "maxLength": MAX_FIELD_CHARS,
            "description": "One or two sentences explaining the severity choice.",
        },
        "missing_evidence": {
            "type": "array",
            "items": {"type": "string", "maxLength": MAX_LIST_ITEM_CHARS},
            "maxItems": MAX_LIST_ITEMS,
            "description": "Evidence absent from the input that would help triage.",
        },
    },
    "required": [
        "title",
        "summary",
        "reproduction_steps",
        "expected_behavior",
        "actual_behavior",
        "severity",
        "severity_rationale",
        "missing_evidence",
    ],
    "additionalProperties": False,
}

REQUIRED_OUTPUT_FIELDS = tuple(BUG_REPORT_OUTPUT_SCHEMA["required"])
