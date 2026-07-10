"""Map Jira issues into store rows and run incremental syncs.

Pure of any Hermes or intra-plugin imports so it can be unit-tested with a
fake client and an in-memory store.  Redaction is intentionally *not* applied
here: the local index holds the full record, and PII is redacted on the
model-facing read paths (prefetch / system prompt / tool results).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
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


def _in_double_quotes(s: str, pos: int) -> bool:
    """True when index *pos* of *s* falls inside a double-quoted region.

    Simple quote-state scan honouring JQL's backslash escapes, so a literal
    ``\\"`` inside a quoted string does not toggle the state.
    """
    in_q = False
    i = 0
    while i < pos:
        ch = s[i]
        if in_q and ch == "\\":
            i += 2  # skip the escaped character
            continue
        if ch == '"':
            in_q = not in_q
        i += 1
    return in_q


def _split_order_by(jql: str) -> tuple[str, str]:
    """Split *jql* into (where, order-by) at the real trailing ORDER BY.

    The split point is the LAST ``order by`` occurrence outside double-quoted
    regions — a quoted string literal like ``summary !~ "order by"`` must not
    be split mid-string (that would splice the watermark into the literal and
    produce permanently rejected JQL). Returns ``(jql, "")`` when there is no
    unquoted ORDER BY.
    """
    last = -1
    for m in re.finditer(r"\border\s+by\b", jql, flags=re.IGNORECASE):
        if not _in_double_quotes(jql, m.start()):
            last = m.start()
    if last < 0:
        return jql, ""
    return jql[:last].strip(), jql[last:].strip()


def apply_watermark(jql: str, watermark: str | None) -> str:
    """Inject an ``updated >= "<watermark>"`` clause, preserving any ORDER BY."""
    jql = (jql or "").strip()
    jql_dt = iso_to_jql(watermark) if watermark else None
    if not jql_dt:
        return jql
    clause = f'updated >= "{jql_dt}"'
    # Split off the trailing ORDER BY (case-insensitive, quote-aware) so the
    # clause lands in the WHERE portion, not after the sort.
    where, order = _split_order_by(jql)
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
# Incremental-resume state (mirrors the backfill pair): an incremental run that
# stops at max_pages persists its nextPageToken plus the newest `updated` seen
# so far, and only advances the real watermark once a run exhausts the result
# set. (Results arrive ORDER BY updated DESC, so advancing the watermark after
# a truncated run would permanently skip the unfetched older-but-still-new
# issues.)
_META_SYNC_TOKEN = "sync_token"
_META_SYNC_NEWEST = "sync_pending_newest"
# The configured JQL the persisted resume tokens were minted against. Jira
# nextPageTokens are query-bound (and expire), so an edited ``jira.jql``
# invalidates them; storing the JQL lets the next run detect the change and
# clear the tokens instead of 400-failing forever.
_META_JQL = "sync_jql"
# The store meta key holding the incremental watermark (see store.get_watermark).
_META_WATERMARK = "sync_watermark"


def reset_sync_state(store: Any) -> None:
    """Clear the watermark and all backfill/resume state on *store*.

    After this, the next :func:`run_sync` re-ingests everything from the first
    page (used by ``flaky-stab jira sync --full``).
    """
    for key in (_META_WATERMARK, _META_BACKFILL_DONE, _META_BACKFILL_TOKEN,
                _META_SYNC_TOKEN, _META_SYNC_NEWEST):
        store.set_meta(key, "")


def _is_client_error(exc: Exception) -> bool:
    """True for a JiraError-shaped exception carrying a 4xx HTTP status.

    Duck-typed on the ``status`` attribute so this module keeps its no-intra-
    plugin-imports property (it unit-tests with a fake client alone).
    """
    status = getattr(exc, "status", None)
    return isinstance(status, int) and 400 <= status < 500


@dataclass
class _ResumeState:
    """Where a sync run starts: which query, which page, what it already knew.

    ``in_incremental`` is the mode switch. It is resolved once, here, rather
    than re-derived at each decision point — the paging loop and the two
    persistence tails all branch on this single field.
    """

    effective_jql: str
    next_token: str | None
    token_meta: str
    pending_newest: str | None
    in_incremental: bool
    watermark: str | None
    backfill_done: bool


@dataclass
class _PageSweep:
    """What one :func:`_sweep_pages` call actually fetched.

    ``exhausted`` means the result set ran out (no issues, or no next token) as
    opposed to stopping at ``max_pages`` — the distinction that decides whether
    the watermark may advance.
    """

    ingested: int = 0
    pages: int = 0
    newest: str | None = None
    next_token: str | None = None
    exhausted: bool = False
    seen_keys: set[str] = field(default_factory=set)


def _clear_tokens_if_query_changed(store: Any, jql: str) -> None:
    """Drop persisted resume tokens when *jql* differs from the last sync's.

    Resume tokens are query-bound; an edited ``jira.jql`` would otherwise make
    every future run 400-fail on a dead token forever.
    """
    stored_jql = store.get_meta(_META_JQL) or ""
    if stored_jql and stored_jql != jql:
        if store.get_meta(_META_BACKFILL_TOKEN) or store.get_meta(_META_SYNC_TOKEN):
            logger.warning(
                "jira.jql changed since the last sync; clearing persisted "
                "resume tokens and restarting pagination from the first page")
        store.set_meta(_META_BACKFILL_TOKEN, "")
        store.set_meta(_META_SYNC_TOKEN, "")
        store.set_meta(_META_SYNC_NEWEST, "")
    store.set_meta(_META_JQL, jql)


def _resolve_resume_state(store: Any, jql: str, incremental: bool) -> _ResumeState:
    """Decide which phase this run is in and where in the result set it resumes."""
    watermark = store.get_watermark() if incremental else None
    # Keyed solely on the explicit flag: a partial backfill also advances the
    # watermark (to retain the global-newest timestamp across runs), so the
    # watermark's presence must NOT by itself imply the backfill is complete.
    backfill_done = store.get_meta(_META_BACKFILL_DONE) == "1"
    in_incremental = incremental and backfill_done

    if in_incremental:
        # Resume a previously truncated incremental run from its token; the
        # watermark filter is identical because the watermark did not advance.
        return _ResumeState(
            effective_jql=apply_watermark(jql, watermark),
            next_token=store.get_meta(_META_SYNC_TOKEN) or None,
            token_meta=_META_SYNC_TOKEN,
            pending_newest=store.get_meta(_META_SYNC_NEWEST) or None,
            in_incremental=True,
            watermark=watermark,
            backfill_done=backfill_done,
        )
    # Initial / resuming backfill: no watermark filter; resume from token.
    return _ResumeState(
        effective_jql=jql,
        next_token=store.get_meta(_META_BACKFILL_TOKEN) or None,
        token_meta=_META_BACKFILL_TOKEN,
        pending_newest=None,
        in_incremental=False,
        watermark=watermark,
        backfill_done=backfill_done,
    )


def _sweep_pages(
    store: Any,
    client: Any,
    resume: _ResumeState,
    *,
    fields: list[str] | None,
    root_cause_field: str | None,
    page_size: int,
    max_pages: int,
) -> _PageSweep:
    """Page through Jira from *resume* until exhaustion or ``max_pages``."""
    sweep = _PageSweep(newest=resume.watermark, next_token=resume.next_token)
    if resume.pending_newest and (sweep.newest is None or resume.pending_newest > sweep.newest):
        sweep.newest = resume.pending_newest

    resumed_from_persisted_token = sweep.next_token is not None

    while sweep.pages < max_pages:
        try:
            resp = client.search(
                resume.effective_jql, fields=fields, max_results=page_size,
                next_page_token=sweep.next_token,
            )
        except Exception as e:
            if resumed_from_persisted_token and sweep.pages == 0 and _is_client_error(e):
                # The persisted resume token went stale (Jira expires them, and
                # server-side query changes invalidate them). Clear it and
                # restart this phase from the first page — otherwise every
                # future run re-reads the same dead token and 400-fails before
                # persisting anything.
                logger.warning(
                    "persisted Jira resume token rejected (%s); clearing it "
                    "and restarting from the first page", e)
                store.set_meta(resume.token_meta, "")
                sweep.next_token = None
                resumed_from_persisted_token = False
                continue
            raise
        issues = resp.get("issues", []) if isinstance(resp, dict) else []
        if not issues:
            sweep.exhausted = True
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
        sweep.ingested += store.upsert_many(mapped)
        sweep.seen_keys.update(row["key"] for row in mapped if row.get("key"))
        sweep.pages += 1

        page_newest = _max_updated(mapped)
        if page_newest and (sweep.newest is None or page_newest > sweep.newest):
            sweep.newest = page_newest

        sweep.next_token = resp.get("nextPageToken") if isinstance(resp, dict) else None
        if not sweep.next_token:
            sweep.exhausted = True
            break
    return sweep


def _persist_incremental_progress(store: Any, sweep: _PageSweep, max_pages: int) -> None:
    """Advance the watermark only if the run saw everything down to the old one.

    Results arrive newest-first, so a run truncated at ``max_pages`` that moved
    the watermark would skip its unfetched tail forever. Such a run persists its
    resume token and the newest ``updated`` seen instead, and the next run picks
    up from there under the same watermark filter.
    """
    if sweep.exhausted:
        if sweep.newest:
            store.set_watermark(sweep.newest)
        store.set_meta(_META_SYNC_TOKEN, "")
        store.set_meta(_META_SYNC_NEWEST, "")
        return
    store.set_meta(_META_SYNC_TOKEN, sweep.next_token or "")
    store.set_meta(_META_SYNC_NEWEST, sweep.newest or "")
    logger.info(
        "incremental sync paused after %d page(s) (max_pages=%d); "
        "watermark held back, will resume next sync", sweep.pages, max_pages,
    )


def _persist_backfill_progress(store: Any, sweep: _PageSweep, max_pages: int) -> None:
    """Record backfill progress; mark the phase done only on full exhaustion.

    Advancing the watermark early is safe here because it is not used for
    filtering until the backfill is marked done.
    """
    if sweep.newest:
        store.set_watermark(sweep.newest)
    if sweep.exhausted:
        store.set_meta(_META_BACKFILL_DONE, "1")
        store.set_meta(_META_BACKFILL_TOKEN, "")
        return
    store.set_meta(_META_BACKFILL_TOKEN, sweep.next_token or "")
    logger.info(
        "backfill paused after %d page(s) (max_pages=%d); will resume "
        "next sync", sweep.pages, max_pages,
    )


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
      exists), only issues ``updated >=`` the watermark are fetched. A run that
      stops at ``max_pages`` does NOT advance the watermark (results arrive
      newest-first, so that would skip the unfetched tail forever); instead it
      persists its ``nextPageToken`` and the newest ``updated`` seen, and the
      next run resumes from that token under the same watermark filter. The
      watermark only advances once a run exhausts the result set.

    Resume tokens are query-bound and expire server-side: a change to *jql*
    clears them proactively, and a 4xx from Jira on a persisted token clears it
    and restarts that phase from the first page (logged as a warning).

    Returns ``{"ingested", "pages", "watermark", "backfill_complete",
    "seen_keys"}`` where ``ingested`` counts rows that were new or actually
    changed (unchanged re-fetches don't count) and ``seen_keys`` is the set of
    issue keys fetched during this run (used by the CLI's ``--full`` sweep).
    Per-issue mapping errors are swallowed; transport/client errors propagate
    to the caller (the provider's background thread, which logs and continues).
    """
    jql = (jql or "").strip()
    _clear_tokens_if_query_changed(store, jql)
    resume = _resolve_resume_state(store, jql, incremental)
    sweep = _sweep_pages(
        store, client, resume,
        fields=fields, root_cause_field=root_cause_field,
        page_size=page_size, max_pages=max_pages,
    )
    if resume.in_incremental:
        _persist_incremental_progress(store, sweep, max_pages)
    else:
        _persist_backfill_progress(store, sweep, max_pages)

    return {
        "ingested": sweep.ingested,
        "pages": sweep.pages,
        "watermark": sweep.newest,
        "backfill_complete": resume.backfill_done or sweep.exhausted,
        "seen_keys": sweep.seen_keys,
    }
