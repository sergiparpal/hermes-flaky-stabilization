"""NET-NEW ``find_duplicate_incidents`` (plan D9).

The honest version of the proposal's fabricated ``search_possible_duplicates``:
a purely local lookup against the incidents index — FTS OR-query (bm25-ranked
via ``IncidentStore.search``) re-ranked by token-overlap ratio between the
candidate summary and the query. No tracker round-trip. Results are redacted
(key/summary/status only) before they leave the store.
"""

from __future__ import annotations

import re
from typing import Any

from ..pii import redaction

DEFAULT_LIMIT = 5
LIMIT_MAX = 20
# Overfetch factor: bm25 order and overlap order disagree at the margin, so
# rank a slightly wider candidate pool before cutting to `limit`.
_POOL_FACTOR = 3

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 2}


def _overlap_ratio(query_tokens: set[str], candidate_text: str) -> float:
    if not query_tokens:
        return 0.0
    candidate_tokens = _tokens(candidate_text)
    if not candidate_tokens:
        return 0.0
    return len(query_tokens & candidate_tokens) / len(query_tokens)


def find_duplicate_incidents(store, summary: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Return ``{candidates: [{key, summary, status, score}], count}`` (redacted).

    ``score`` is the token-overlap ratio (0..1, 3 decimals) between the query
    and the candidate's summary+body; candidates are bm25-preranked by the
    store's FTS search, then re-ranked by score (stable, so bm25 breaks ties).
    """
    try:
        limit = max(1, min(int(limit), LIMIT_MAX))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    query = (summary or "").strip()
    if not query or store is None:
        return {"candidates": [], "count": 0}

    query_tokens = _tokens(query)
    rows = store.search(query, limit=limit * _POOL_FACTOR)
    scored = []
    for row in rows:
        score = _overlap_ratio(
            query_tokens, f"{row.get('summary', '')} {row.get('body', '')}"
        )
        scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)  # stable: bm25 breaks ties

    candidates = []
    for score, row in scored[:limit]:
        safe = redaction.redact_incident(row, fields=("key", "summary", "status"))
        candidates.append({
            "key": safe.get("key", ""),
            "summary": safe.get("summary", ""),
            "status": safe.get("status", ""),
            "score": round(score, 3),
        })
    return {"candidates": candidates, "count": len(candidates)}
