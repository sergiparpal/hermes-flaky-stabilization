"""PII stage: evidence scanning + egress redaction (two jobs, two modules).

* ``detectors``/``scanner`` (ported from hermes-masking-validator) — the
  read-only **evidence report**: scan files/dirs for residual PII before they
  leave the machine; masked previews only, never mutates anything.
* ``redaction`` (ported from hermes-jira-incidents) — the **egress rewrite**:
  scrub text that is about to reach the model or an external tracker.

They deliberately share no code (plan §4.7): a validator that reports and a
rewriter that scrubs have different failure modes and different contracts.

``register(ctx)`` wires the ``validate_no_pii`` tool (toolset ``qa_masking``)
verbatim from the legacy plugin, minus its dual-import shim — the unified
package gives these modules a stable import root.
"""

import copy
import json
import logging

from .detectors import DETECTOR_NAMES
from .scanner import scan

logger = logging.getLogger("hermes.plugins.masking_validator")

TOOLSET = "qa_masking"

SCHEMA = {
    "name": "validate_no_pii",
    "description": (
        "Scan a file or directory of QA evidence for residual PII (personal data) "
        "before attaching it to an external ticket (Jira, Bugzilla, etc.). Returns "
        "findings with masked previews (e.g. '************1234'); never returns raw "
        "PII. Read-only: it does not modify, redact, or write anything. Use this as a "
        "verification gate right before uploading logs/screenshots to a public tracker."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Absolute or relative path to a file or directory to scan.",
            },
            # 'items' gains an 'enum' of the live detector names at register()
            # time — see _schema_with_detector_enum.
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of detector names to run "
                    "(e.g. ['email', 'credit_card']). Defaults to all configured detectors."
                ),
            },
            "max_files": {
                "type": "integer",
                "description": "Optional cap on the number of files scanned (safety bound).",
            },
        },
        "required": ["target"],
    },
}


def _handle_validate_no_pii(params, **kwargs):
    """Delegate to ``scanner.scan`` and return a JSON-encoded string.

    Per the handler contract: always returns ``json.dumps(...)`` (never a dict),
    validates input before any side effect, and never leaks a stack trace or raw
    PII to the model. Logging records finding *types + counts + locations* only.
    """
    try:
        if not isinstance(params, dict):
            return json.dumps({
                "success": False,
                "error": "params must be a JSON object.",
                "remediation": "Call the tool with an object containing a 'target' field.",
            })

        result = scan(
            params.get("target"),
            params.get("types"),
            params.get("max_files"),
        )

        # Redacted logging: never log raw PII or masked previews — only the
        # aggregate shape (types + counts + how many files/skips).
        if result.get("success"):
            logger.info(
                "validate_no_pii: clean=%s complete=%s scanned=%s summary=%s "
                "skipped=%d truncated=%s",
                result.get("clean"),
                result.get("complete"),
                result.get("scanned_files"),
                result.get("summary", {}),
                len(result.get("skipped", [])),
                result.get("truncated"),
            )
        else:
            logger.info("validate_no_pii: error=%s", result.get("error"))

        return json.dumps(result)
    except Exception as exc:  # never leak stack/PII to the model
        logger.warning("validate_no_pii failed: %s", type(exc).__name__)
        return json.dumps({
            "success": False,
            "error": f"scan failed: {type(exc).__name__}",
            "remediation": "Check that 'target' is a readable file or directory.",
        })


def _schema_with_detector_enum():
    """``SCHEMA`` with the live detector names injected as an enum on ``types``.

    Keeps ``detectors.DETECTOR_NAMES`` the single source of truth — the schema
    cannot drift from it — and lets an invalid detector name be rejected at the
    tool-call layer instead of round-tripping through a structured error.
    """
    schema = copy.deepcopy(SCHEMA)
    schema["parameters"]["properties"]["types"]["items"]["enum"] = list(DETECTOR_NAMES)
    return schema


def register(ctx):
    """Register ``validate_no_pii`` under the ``qa_masking`` toolset."""
    ctx.register_tool(
        name="validate_no_pii",
        toolset=TOOLSET,
        schema=_schema_with_detector_enum(),
        handler=_handle_validate_no_pii,
    )
