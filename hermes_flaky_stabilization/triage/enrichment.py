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
from typing import Any

from . import prefilter

logger = logging.getLogger(__name__)

# Cap on the single signal line used to seed enrichment lookups.
_SIGNAL_LINE_CHARS = 200

# Failure events requested from the history lookup (a prompt hint, not a report).
_ENRICH_LIMIT = 5

_SOURCE = "test_failure_lookup"


def _top_signal_line(excerpt: str) -> str:
    """Pick the most representative line of *excerpt* to seed the lookup query."""
    for line in (excerpt or "").split("\n"):
        if prefilter.is_failure_line(line):
            return line.strip()[:_SIGNAL_LINE_CHARS]
    return (excerpt or "").strip().split("\n")[0][:_SIGNAL_LINE_CHARS]


def _history_lookup(query: str) -> dict[str, Any]:
    """Run the direct in-package history lookup (isolated for test injection)."""
    from ..history import queries, storage

    conn = storage.get_connection()
    return queries.test_failure_lookup(
        conn, test_id=query, limit=_ENRICH_LIMIT, config=storage.get_config()
    )


def enrich(excerpt: str) -> dict[str, Any] | None:
    """Look the excerpt's top signal line up in test history.

    Returns ``{"source": "test_failure_lookup", "result": <lookup dict>}`` when
    the lookup matched at least one known test, else ``None``. Non-fatal: an
    unreadable history DB, a validation error, or any other exception yields
    ``None``.
    """
    query = _top_signal_line(excerpt)
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
