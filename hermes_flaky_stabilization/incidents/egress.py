"""Model-facing egress safety for the incident tools.

Extracted from the ``IncidentsService`` god-object: the defense-in-depth PII
canary and the JSON result funnel are one concern (surface residual PII /
injection markers on the model-facing path, and encode every tool result the
same way), independent of the service's connection / sync / config
responsibilities. Pure functions over :mod:`redaction` — no service state.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..pii import redaction
from .config import NAME

logger = logging.getLogger(__name__)

# When this env var is truthy, model-facing output is re-scanned as a canary.
STRICT_REDACTION_ENV = "HERMES_JIRA_STRICT_REDACTION"


def strict_enabled() -> bool:
    return os.environ.get(STRICT_REDACTION_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def guard(text: str, where: str) -> str:
    """Defense-in-depth canary on every model-facing string.

    When strict mode is enabled, re-scan already-redacted output and log a
    warning if anything still looks like PII *or* like un-neutralised injection
    residue (a control char / forged role marker) — both signs a redactor or the
    neutraliser missed a case. Never raises and never mutates the payload —
    redaction already ran; this only surfaces gaps so they can be fixed.
    """
    try:
        if text and strict_enabled() and (
                redaction.contains_pii(text)
                or redaction.neutralization_needed(text)):
            logger.warning(
                "%s: possible residual PII or injection marker on egress "
                "path '%s'", NAME, where)
    except Exception:
        pass
    return text


def json_result(payload: dict[str, Any], where: str) -> str:
    """Serialise a tool-result payload and run it through the egress canary.

    One funnel for every tool result so they all share the same JSON encoding
    (``ensure_ascii=False``) and the same PII re-scan — a new tool can't silently
    skip the guard or drop the flag.
    """
    return guard(json.dumps(payload, ensure_ascii=False), where)
