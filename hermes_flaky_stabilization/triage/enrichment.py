"""Triage enrichment via the in-package test-history stage (bug fixed).

The legacy plugin dispatched the partner tools ``test_failure_lookup`` /
``module_failure_history`` with the argument key ``"query"`` — but those tools
require ``test_id`` / ``path``, so every call came back as a
``{"success": false, ...}`` validation envelope that the unknown-tool filter
did not catch, and that useless envelope was injected into the LLM prompt
(plan §2 row 6, ``hermes-ci-triage/enrichment.py:38-41``). Now that history is
a sibling package, the lookup is a direct function call with the right
arguments; the FTS fallback inside ``queries.test_failure_lookup`` makes
free-text signal lines work.

Non-fatal semantics preserved: any absence or error returns ``None``, and a
lookup that matches nothing returns ``None`` too (an empty result is not a
useful prompt hint). The returned object is *not* redacted — the orchestrator
scrubs it before it is fed to the LLM or echoed back; redaction stays a single
chokepoint in ``handlers``.

Pure standard library; depends on the sibling ``prefilter`` for failure-line
detection and on the in-package ``history`` stage for data.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from . import prefilter

logger = logging.getLogger(__name__)

# Cap on the single signal line used to seed enrichment lookups.
_SIGNAL_LINE_CHARS = 200

# Failure events requested from the history lookup (a prompt hint, not a report).
_ENRICH_LIMIT = 5

_SOURCE = "test_failure_lookup"

# Prefilter-injected marker lines ("... (N lines omitted) ..." and the
# "⟪ previous line repeated N more times ⟫" collapse marker).
_PREFILTER_MARKER_RE = re.compile(
    r"^\s*(?:\.\.\. \(\d+ lines omitted\) \.\.\."
    r"|⟪ previous line repeated \d+ more times? ⟫)\s*$"
)


def _is_synthetic_line(line: str) -> bool:
    """True for lines the prefilter injected rather than took from the log.

    The truncation note contains the word "failure", so
    ``prefilter.is_failure_line`` matches it — without this guard every
    truncated excerpt would seed the history lookup with the note itself
    (it is the excerpt's first line) and enrichment would silently never
    work on exactly the big logs it matters for.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(prefilter.TRUNCATION_NOTE):
        return True
    return bool(_PREFILTER_MARKER_RE.match(line))


def top_signal_line(excerpt: str) -> str:
    """Pick the most representative line of *excerpt* to seed the lookup query.

    Public seam: the orchestrator reuses this to derive its incident-context
    query from the triage excerpt."""
    lines = (excerpt or "").split("\n")
    for line in lines:
        if _is_synthetic_line(line):
            continue
        if prefilter.is_failure_line(line):
            return line.strip()[:_SIGNAL_LINE_CHARS]
    for line in lines:
        stripped = line.strip()
        if stripped and not _is_synthetic_line(line):
            return stripped[:_SIGNAL_LINE_CHARS]
    return ""


def _history_lookup(query: str) -> dict[str, Any]:
    """Run the history lookup through its public port (isolated for test injection).

    Depends on ``history.failure_history`` — the stage's stable public surface —
    not on history's internal ``queries``/``storage`` modules.
    """
    from ..history import failure_history

    return failure_history(query, limit=_ENRICH_LIMIT)


def enrich(excerpt: str) -> dict[str, Any] | None:
    """Look the excerpt's top signal line up in test history.

    Returns ``{"source": "test_failure_lookup", "result": <lookup dict>}`` when
    the lookup matched at least one known test, else ``None``. Non-fatal: an
    unreadable history DB, a validation error, or any other exception yields
    ``None``.
    """
    query = top_signal_line(excerpt)
    if not query:
        return None
    try:
        result = _history_lookup(query)
    except Exception:
        logger.debug("history enrichment failed", exc_info=True)
        return None
    if not isinstance(result, dict) or not result.get("matched_tests"):
        return None
    return {"source": _SOURCE, "result": result}
