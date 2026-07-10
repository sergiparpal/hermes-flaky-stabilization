"""Redacted, read-only view over the incident index.

The incident store holds full, unredacted records at rest (emails, names, any
secret pasted into a ticket). The service's own tools redact before emitting,
but external callers (the orchestrator's dedup and incident-context lookups)
used to receive the *raw* ``IncidentStore`` and each had to remember to redact —
redaction by discipline, one forgotten call from a leak.

:class:`IncidentReader` is the structural fix: it is the ONLY incident surface
handed to model-facing callers, and every value it returns is already redacted.
The raw store stays private to the service (ingest, sync) and to this reader.
"""

from __future__ import annotations

from typing import Any

from ..pii import redaction

# The projection every model-facing search returns — nothing else leaves.
_SEARCH_FIELDS = ("key", "summary", "status")


class IncidentReader:
    """Read-only incident lookups whose results are already PII-redacted.

    Wraps the (possibly ``None``) live store; every method degrades to an empty
    result when the index is unavailable, so callers never null-check.
    """

    def __init__(self, store: Any | None):
        self._store = store

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        """Redacted ``key``/``summary``/``status`` projections for *query*."""
        if self._store is None:
            return []
        rows = self._store.search(query, limit=limit)
        return [self._project(row) for row in rows]

    def find_duplicates(self, summary: str, limit: int = 5) -> dict[str, Any]:
        """Likely-duplicate incidents for *summary* (redacted candidates + score).

        Delegates to the incident-dedup ranking, which scores against the raw
        rows internally but emits only redacted key/summary/status + a numeric
        score — so the raw body is used for matching yet never reaches a caller.
        """
        from . import dedup

        return dedup.find_duplicate_incidents(self._store, summary, limit)

    @staticmethod
    def _project(row: dict[str, Any]) -> dict[str, str]:
        safe = redaction.redact_incident(row, fields=_SEARCH_FIELDS)
        return {field: safe.get(field, "") for field in _SEARCH_FIELDS}
