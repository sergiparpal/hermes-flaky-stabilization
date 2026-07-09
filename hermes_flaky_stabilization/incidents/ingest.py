"""Map Jira issues into store rows and run incremental syncs.

Pure of any Hermes or intra-plugin imports so it can be unit-tested with a
fake client and an in-memory store.  Redaction is intentionally *not* applied
here: the local index holds the full record, and PII is redacted on the
model-facing read paths (prefetch / system prompt / tool results).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Custom-field ids/names commonly used for root-cause / RCA across Jira setups.
_RCA_FIELD_HINTS = ("root_cause", "root cause", "rca", "cause", "post-mortem", "postmortem")

# Non-alphanumeric run collapser, used to normalise field ids/names for the
# token-boundary RCA hint match.
_NORM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_hint(s: str) -> str:
    """Lower-case *s* and collapse non-alphanumeric runs to single spaces,
    padded with spaces so membership tests match on token boundaries."""
    return " " + _NORM_RE.sub(" ", s.lower()).strip() + " "


# Precomputed once: the normalised hint forms. Recomputing these per field per
# issue during ingest was pure repeated work (issues × fields × hints).
_RCA_HINTS_NORM = tuple(_normalize_hint(h) for h in _RCA_FIELD_HINTS)


# ---------------------------------------------------------------------------
# Atlassian Document Format (ADF) flattening
# ---------------------------------------------------------------------------

# ADF can in principle nest arbitrarily; bound the recursion so a pathological
# (or malicious) document cannot blow the Python stack.
_ADF_MAX_DEPTH = 100


def adf_to_text(node: Any, _depth: int = 0) -> str:
    """Flatten an ADF node (Jira Cloud v3 rich text) to plain text.

    Tolerates the v2 case where the field is already a plain string, and any
    unexpected shape (returns ""). Recursion is depth-bounded for safety.
    """
    if node is None or _depth > _ADF_MAX_DEPTH:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n, _depth + 1) for n in node)
    if not isinstance(node, dict):
        return str(node)

    parts: list[str] = []
    ntype = node.get("type")
    if ntype == "text":
        parts.append(node.get("text", ""))
    # Recurse into content regardless of node type.
    if node.get("content"):
        parts.append(adf_to_text(node["content"], _depth + 1))
    # Block-level nodes get a trailing newline for readability.
    if ntype in ("paragraph", "heading", "blockquote", "listItem", "codeBlock", "rule"):
        parts.append("\n")
    return "".join(parts)


def field_to_text(value: Any) -> str:
    """Best-effort flatten of an arbitrary Jira field value to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        # ADF document?
        if value.get("type") == "doc" or "content" in value:
            return adf_to_text(value).strip()
        # Common Jira shapes: {"value": ...}, {"name": ...}, {"displayName": ...}
        for k in ("value", "name", "displayName", "text"):
            if k in value and isinstance(value[k], str):
                return value[k].strip()
        return ""
    if isinstance(value, list):
        return ", ".join(t for t in (field_to_text(v) for v in value) if t)
    return str(value)


# ---------------------------------------------------------------------------
# Issue → store row mapping
# ---------------------------------------------------------------------------

def _person_name(field_value: Any) -> str:
    if isinstance(field_value, dict):
        return field_value.get("displayName") or field_value.get("name") or ""
    return field_to_text(field_value)


def _field_name_hints_rca(field_id: str) -> bool:
    """True when a field id/name looks like a root-cause/RCA field.

    Matching is on token boundaries against a normalised name (non-alphanumeric
    runs collapsed to single spaces), so "Root Cause"/"root_cause"/"RCA" match
    while "because"/"customfield_10050" do not.
    """
    norm = _normalize_hint(field_id)
    return any(hint_norm in norm for hint_norm in _RCA_HINTS_NORM)


def extract_root_cause(fields: dict[str, Any], root_cause_field: str | None, body: str) -> str:
    """Resolve the root-cause text for an issue.

    Priority:
      1. The explicitly configured ``root_cause_field`` (a field id/name), if
         present and non-empty.
      2. Any field whose id/name hints at root-cause/RCA.
      3. A "root cause" section parsed out of the body text.
      4. "" (caller may choose to fall back to the body/description).

    Note on step 2: the Jira search API keys custom fields by id
    (``customfield_10050``), not by their human-readable name, so the hint
    match only fires for standard/named fields. A *custom* RCA field is not
    auto-detected — point ``root_cause_field`` at it explicitly (step 1).
    """
    if not isinstance(fields, dict):
        fields = {}

    # 1. Configured field.
    if root_cause_field and root_cause_field in fields:
        text = field_to_text(fields[root_cause_field])
        if text:
            return text

    # 2. Heuristic field match by id/name, on token boundaries so a hint like
    #    "cause" matches a "Root Cause" field but not the word "because".
    for fid, value in fields.items():
        if _field_name_hints_rca(str(fid)):
            text = field_to_text(value)
            if text:
                return text

    # 3. Parse a "Root cause:" section out of the body.
    if body:
        m = re.search(r"root\s*cause\s*[:\-]\s*(.+)", body, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip().split("\n")[0].strip()

    return ""


def map_issue(issue: dict[str, Any], root_cause_field: str | None = None) -> dict[str, Any]:
    """Map a raw Jira issue dict into an :class:`IncidentStore` row dict."""
    if not isinstance(issue, dict):
        raise ValueError("issue must be a dict")
    key = issue.get("key") or ""
    fields = issue.get("fields") or {}

    status = ""
    status_field = fields.get("status")
    if isinstance(status_field, dict):
        status = status_field.get("name", "") or ""
    else:
        status = field_to_text(status_field)

    body = field_to_text(fields.get("description"))
    root_cause = extract_root_cause(fields, root_cause_field, body)
    if not root_cause:
        # Fall back to the description so jira_get_root_cause is still useful.
        root_cause = body

    return {
        "key": key,
        "summary": field_to_text(fields.get("summary")),
        "status": status,
        "root_cause": root_cause,
        "reporter": _person_name(fields.get("reporter")),
        "assignee": _person_name(fields.get("assignee")),
        "created": field_to_text(fields.get("created")),
        "updated": field_to_text(fields.get("updated")),
        "body": body,
        # The full raw issue is no longer persisted: nothing reads it back, and
        # serialising every issue (incl. its ADF description) on the ingest hot
        # path was pure CPU + storage cost. The flattened fields above are the
        # source of truth the store and tools use.
        "raw_json": "",
    }


# ---------------------------------------------------------------------------
# Incremental sync
# ---------------------------------------------------------------------------

def iso_to_jql(iso_ts: str) -> str | None:
    """Convert an ISO-ish timestamp into Jira's JQL datetime ('yyyy/MM/dd HH:mm')."""
    if not iso_ts:
        return None
    datetime_match = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", iso_ts)
    if datetime_match:
        year, month, day, hour, minute = datetime_match.groups()
        return f"{year}/{month}/{day} {hour}:{minute}"
    date_only_match = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso_ts)
    if date_only_match:
        year, month, day = date_only_match.groups()
        return f"{year}/{month}/{day} 00:00"
    return None


def apply_watermark(jql: str, watermark: str | None) -> str:
    """Inject an ``updated >= "<watermark>"`` clause, preserving any ORDER BY."""
    jql = (jql or "").strip()
    jql_dt = iso_to_jql(watermark) if watermark else None
    if not jql_dt:
        return jql
    clause = f'updated >= "{jql_dt}"'
    # Split off a trailing ORDER BY (case-insensitive) so the clause lands in
    # the WHERE portion, not after the sort.
    m = re.search(r"\border\s+by\b", jql, flags=re.IGNORECASE)
    if m:
        where, order = jql[: m.start()].strip(), jql[m.start():].strip()
    else:
        where, order = jql, ""
    if where:
        where = f"({where}) AND {clause}"
    else:
        where = clause
    return f"{where} {order}".strip()


def _max_updated(rows: list[dict[str, Any]]) -> str | None:
    # Lexicographic max over Jira's ISO-8601 `updated` strings. Safe because
    # Jira Cloud renders every timestamp in the requesting account's single
    # timezone, so all rows share one offset and string order matches time
    # order. (Mixed offsets would break this — not a case Jira Cloud produces.)
    updated = [r.get("updated") for r in rows if r.get("updated")]
    return max(updated) if updated else None


# meta keys tracking initial-backfill progress (so a project larger than
# max_pages*page_size is indexed completely across successive syncs rather than
# silently truncated to the newest page).
_META_BACKFILL_DONE = "backfill_done"
_META_BACKFILL_TOKEN = "backfill_token"


def run_sync(
    store: Any,
    client: Any,
    jql: str,
    *,
    fields: list[str] | None = None,
    root_cause_field: str | None = None,
    page_size: int = 50,    # see note below
    max_pages: int = 20,    # see note below
    incremental: bool = True,
) -> dict[str, Any]:
    # NOTE: page_size/max_pages defaults are library-level fallbacks only — this
    # module stays import-free (no config dependency) so it unit-tests in
    # isolation. The application's source of truth for these knobs is
    # config.IncidentsConfig; real callers always pass resolved values via
    # IncidentsConfig.run_sync_kwargs(), so these literals are never the
    # effective values in production.
    """Page through Jira search results and upsert them into the store.

    Pagination is token-based (``nextPageToken``). Two modes:

    * **Backfill** — until the whole result set has been seen at least once,
      successive runs resume from a persisted ``nextPageToken`` (no watermark
      filter), so a backlog larger than ``max_pages*page_size`` is indexed
      completely across several syncs instead of being truncated to the newest
      page. On full exhaustion the backfill is marked done.
    * **Incremental** — once backfilled (or whenever a watermark already
      exists), only issues ``updated >=`` the watermark are fetched.

    Returns ``{"ingested", "pages", "watermark", "backfill_complete"}`` where
    ``ingested`` counts rows that were new or actually changed (unchanged
    re-fetches don't count). Per-issue mapping errors are swallowed;
    transport/client errors propagate to the caller (the provider's background
    thread, which logs and continues).
    """
    watermark = store.get_watermark() if incremental else None
    # Keyed solely on the explicit flag: a partial backfill also advances the
    # watermark (to retain the global-newest timestamp across runs), so the
    # watermark's presence must NOT by itself imply the backfill is complete.
    backfill_done = store.get_meta(_META_BACKFILL_DONE) == "1"

    if incremental and backfill_done:
        effective_jql = apply_watermark(jql, watermark)
        next_token: str | None = None
    else:
        # Initial / resuming backfill: no watermark filter; resume from token.
        effective_jql = (jql or "").strip()
        next_token = store.get_meta(_META_BACKFILL_TOKEN) or None

    ingested = 0
    pages = 0
    newest = watermark
    exhausted = False

    while pages < max_pages:
        resp = client.search(
            effective_jql, fields=fields, max_results=page_size,
            next_page_token=next_token,
        )
        issues = resp.get("issues", []) if isinstance(resp, dict) else []
        if not issues:
            exhausted = True
            break
        mapped: list[dict[str, Any]] = []
        for issue in issues:
            try:
                mapped.append(map_issue(issue, root_cause_field))
            except Exception as e:
                logger.debug("skipping unmappable issue: %s", e)
        # upsert_many returns the count of rows that were actually new or
        # changed (unchanged re-fetches are skipped), so "ingested" reflects
        # real index change — not the boundary issues every incremental sync
        # re-fetches and re-upserts idempotently.
        ingested += store.upsert_many(mapped)
        pages += 1

        page_newest = _max_updated(mapped)
        if page_newest and (newest is None or page_newest > newest):
            newest = page_newest

        next_token = resp.get("nextPageToken") if isinstance(resp, dict) else None
        if not next_token:
            exhausted = True
            break

    if newest:
        store.set_watermark(newest)

    # Persist backfill progress so the next run resumes where this one stopped.
    if not backfill_done:
        if exhausted:
            store.set_meta(_META_BACKFILL_DONE, "1")
            store.set_meta(_META_BACKFILL_TOKEN, "")
        else:
            store.set_meta(_META_BACKFILL_TOKEN, next_token or "")
            logger.info(
                "backfill paused after %d page(s) (max_pages=%d); will resume "
                "next sync", pages, max_pages,
            )

    return {
        "ingested": ingested,
        "pages": pages,
        "watermark": newest,
        "backfill_complete": backfill_done or exhausted,
    }
