"""Ingest mapping + incremental sync tests (plan §3). No network."""

import logging
import os
import re

import pytest
from jira_incidents import ingest
from jira_incidents.store import IncidentStore

# ---------------------------------------------------------------------------
# ADF flattening + field mapping
# ---------------------------------------------------------------------------

def test_adf_to_text():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Disk filled up."}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Root cause: log rotation off."}]},
        ],
    }
    text = ingest.adf_to_text(doc)
    assert "Disk filled up." in text
    assert "Root cause: log rotation off." in text


def test_adf_handles_plain_string():
    assert ingest.adf_to_text("plain v2 description") == "plain v2 description"


def test_field_to_text_shapes():
    assert ingest.field_to_text({"name": "In Progress"}) == "In Progress"
    assert ingest.field_to_text({"value": "High"}) == "High"
    assert ingest.field_to_text(["a", "b"]) == "a, b"
    assert ingest.field_to_text(None) == ""


SAMPLE_ISSUE = {
    "key": "INC-100",
    "fields": {
        "summary": "Checkout 500s",
        "status": {"name": "Resolved"},
        "reporter": {"displayName": "Jane Doe"},
        "assignee": {"displayName": "Bob Roe"},
        "created": "2024-01-01T00:00:00.000+0000",
        "updated": "2024-01-05T12:00:00.000+0000",
        "description": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Orders failed."}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Root cause: bad deploy."}]},
            ],
        },
    },
}


def test_map_issue_basic():
    row = ingest.map_issue(SAMPLE_ISSUE)
    assert row["key"] == "INC-100"
    assert row["summary"] == "Checkout 500s"
    assert row["status"] == "Resolved"
    assert row["reporter"] == "Jane Doe"
    assert row["assignee"] == "Bob Roe"
    assert "Orders failed." in row["body"]


def test_extract_root_cause_from_configured_field():
    fields = {"customfield_10034": "Cable was unplugged", "summary": "x"}
    rc = ingest.extract_root_cause(fields, "customfield_10034", body="")
    assert rc == "Cable was unplugged"


def test_extract_root_cause_heuristic_field_name():
    fields = {"root_cause": "Memory leak in worker"}
    rc = ingest.extract_root_cause(fields, None, body="")
    assert rc == "Memory leak in worker"


def test_extract_root_cause_from_body_section():
    rc = ingest.extract_root_cause({}, None, body="Stuff happened. Root cause: bad deploy.\nmore")
    assert rc == "bad deploy."


def test_map_issue_falls_back_to_body_for_root_cause():
    issue = {"key": "INC-7", "fields": {"summary": "x",
             "description": "No structured RCA here at all"}}
    row = ingest.map_issue(issue)
    assert row["root_cause"] == "No structured RCA here at all"


def test_rca_hint_matches_on_token_boundary_not_substring():
    # "Root Cause" / "rca" match; "because" (contains "cause") does not.
    assert ingest._field_name_hints_rca("Root Cause")
    assert ingest._field_name_hints_rca("root_cause")
    assert ingest._field_name_hints_rca("RCA Notes")
    assert not ingest._field_name_hints_rca("because_field")
    assert not ingest._field_name_hints_rca("customfield_10050")

    # A field merely containing the word "because" is not picked as root cause.
    fields = {"because_we_shipped": "not the rca", "summary": "x"}
    assert ingest.extract_root_cause(fields, None, body="") == ""


def test_adf_depth_is_bounded():
    # Build a doc nested deeper than the recursion guard; must not raise.
    node = {"type": "text", "text": "deep"}
    for _ in range(300):
        node = {"type": "paragraph", "content": [node]}
    out = ingest.adf_to_text(node)
    assert isinstance(out, str)  # bounded, no RecursionError


# ---------------------------------------------------------------------------
# Watermark helpers
# ---------------------------------------------------------------------------

def test_iso_to_jql():
    assert ingest.iso_to_jql("2024-01-05T12:30:45Z") == "2024/01/05 12:30"
    assert ingest.iso_to_jql("2024-01-05") == "2024/01/05 00:00"
    assert ingest.iso_to_jql("") is None


def test_apply_watermark_preserves_order_by():
    out = ingest.apply_watermark("project = INC ORDER BY updated DESC", "2024-01-05T12:30:00Z")
    assert 'updated >= "2024/01/05 12:30"' in out
    assert out.strip().lower().endswith("order by updated desc")
    assert out.lower().index("updated >=") < out.lower().index("order by")


def test_apply_watermark_noop_without_watermark():
    jql = "project = INC ORDER BY updated DESC"
    assert ingest.apply_watermark(jql, None) == jql


def test_apply_watermark_ignores_order_by_inside_quoted_literal():
    # An "order by" inside a quoted string literal must not be treated as the
    # sort clause — splicing the watermark mid-string produced rejected JQL.
    jql = 'summary !~ "order by" ORDER BY updated DESC'
    out = ingest.apply_watermark(jql, "2024-01-05T12:30:00Z")
    assert 'summary !~ "order by"' in out  # literal left intact
    assert out.lower().endswith("order by updated desc")
    # the watermark clause lands before the real (unquoted, last) ORDER BY
    assert out.index('updated >= "2024/01/05 12:30"') < out.lower().rindex("order by")


def test_apply_watermark_appends_when_order_by_only_inside_quotes():
    out = ingest.apply_watermark('summary !~ "order by"', "2024-01-05T12:30:00Z")
    assert out == '(summary !~ "order by") AND updated >= "2024/01/05 12:30"'


# ---------------------------------------------------------------------------
# run_sync with a fake client
# ---------------------------------------------------------------------------

class FakeClient:
    """Records JQL it was asked for; returns a single page (no nextPageToken)."""

    def __init__(self, issues, raise_exc=None):
        self._issues = issues
        self.raise_exc = raise_exc
        self.queries = []

    def search(self, jql, fields=None, max_results=50, next_page_token=None):
        self.queries.append(jql)
        if self.raise_exc:
            raise self.raise_exc
        if next_page_token:  # the single page was already served
            return {"issues": []}
        return {"issues": self._issues}  # no nextPageToken -> last page


class PagingClient:
    """Token-paginated fake: serves `issues` in chunks of `page`.

    The token is just the next start offset encoded as a string; the final
    page omits ``nextPageToken``.
    """

    def __init__(self, issues, page=2):
        self._issues = issues
        self._page = page
        self.calls = []

    def search(self, jql, fields=None, max_results=50, next_page_token=None):
        start = int(next_page_token) if next_page_token else 0
        self.calls.append((jql, start))
        chunk = self._issues[start:start + self._page]
        resp = {"issues": chunk}
        nxt = start + self._page
        if nxt < len(self._issues):
            resp["nextPageToken"] = str(nxt)
        return resp


@pytest.fixture
def store(tmp_home):
    s = IncidentStore(os.path.join(tmp_home, "idx.db"))
    yield s
    s.close()


def test_run_sync_populates_store(store):
    client = FakeClient([SAMPLE_ISSUE])
    result = ingest.run_sync(store, client, "project = INC ORDER BY updated DESC")
    assert result["ingested"] == 1
    assert store.count() == 1
    assert store.get("INC-100")["summary"] == "Checkout 500s"


def test_run_sync_sets_watermark_to_max_updated(store):
    client = FakeClient([SAMPLE_ISSUE])
    ingest.run_sync(store, client, "project = INC")
    assert store.get_watermark() == "2024-01-05T12:00:00.000+0000"


def test_resync_with_no_changes_reports_zero_ingested(store):
    # A steady-state incremental sync re-fetches the watermark-boundary issue
    # but it's unchanged, so "ingested" is 0 — the signal the provider uses to
    # avoid needlessly clearing its prefetch cache (audit finding 3).
    client = FakeClient([SAMPLE_ISSUE])
    assert ingest.run_sync(store, client, "project = INC")["ingested"] == 1
    assert ingest.run_sync(store, client, "project = INC")["ingested"] == 0
    assert store.count() == 1


def test_incremental_sync_uses_watermark_clause(store):
    # A store that has completed its initial backfill syncs incrementally.
    store.set_meta("backfill_done", "1")
    store.set_watermark("2024-01-05T12:00:00Z")
    client = FakeClient([])
    ingest.run_sync(store, client, "project = INC ORDER BY updated DESC")
    # The query actually issued must carry the watermark filter.
    assert any('updated >=' in q for q in client.queries)
    assert any('2024/01/05 12:00' in q for q in client.queries)


def test_run_sync_propagates_client_error(store):
    client = FakeClient([], raise_exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        ingest.run_sync(store, client, "project = INC")


def _issue(i):
    return {"key": f"INC-{i}", "fields": {
        "summary": f"incident {i}",
        "updated": f"2024-01-{i + 1:02d}T00:00:00.000+0000"}}


def test_backfill_resumes_across_runs_until_complete(store):
    """A backlog larger than max_pages*page_size is indexed completely across
    successive syncs, not truncated to the first page."""
    issues = [_issue(i) for i in range(4)]
    client = PagingClient(issues, page=2)

    # max_pages=1, page_size=2 -> only 2 issues fetched per run.
    r1 = ingest.run_sync(store, client, "project = INC", page_size=2, max_pages=1)
    assert store.count() == 2
    assert r1["backfill_complete"] is False
    assert store.get_meta("backfill_token")  # a resume point was persisted

    r2 = ingest.run_sync(store, client, "project = INC", page_size=2, max_pages=1)
    assert store.count() == 4               # the rest got indexed
    assert r2["backfill_complete"] is True
    assert store.get_meta("backfill_done") == "1"
    # Global-newest watermark retained across the multi-run backfill.
    assert store.get_watermark() == "2024-01-04T00:00:00.000+0000"


def test_backfill_then_incremental(store):
    issues = [_issue(i) for i in range(2)]
    client = PagingClient(issues, page=5)  # all in one page
    ingest.run_sync(store, client, "project = INC ORDER BY updated DESC", page_size=5, max_pages=5)
    assert store.get_meta("backfill_done") == "1"

    # Now incremental: the next query must carry the watermark filter.
    client2 = FakeClient([])
    ingest.run_sync(store, client2, "project = INC ORDER BY updated DESC")
    assert any("updated >=" in q for q in client2.queries)


# ---------------------------------------------------------------------------
# Truncated incremental sync: watermark held back + token resume
# ---------------------------------------------------------------------------

class WatermarkPagingClient:
    """Jira-faithful fake: honours the ``updated >= "..."`` watermark clause
    and serves results ORDER BY updated DESC with token pagination (the token
    is the next start offset)."""

    def __init__(self, issues, page=2):
        self._issues = sorted(
            issues, key=lambda i: i["fields"]["updated"], reverse=True)
        self._page = page
        self.calls = []  # (jql, next_page_token)

    def search(self, jql, fields=None, max_results=50, next_page_token=None):
        self.calls.append((jql, next_page_token))
        issues = self._issues
        m = re.search(r'updated >= "([^"]+)"', jql)
        if m:
            cutoff = m.group(1)
            issues = [i for i in issues
                      if ingest.iso_to_jql(i["fields"]["updated"]) >= cutoff]
        start = int(next_page_token) if next_page_token else 0
        chunk = issues[start:start + self._page]
        resp = {"issues": chunk}
        if start + self._page < len(issues):
            resp["nextPageToken"] = str(start + self._page)
        return resp


def test_truncated_incremental_sync_holds_watermark_and_resumes(store):
    """SCENARIO: 6 new issues, max_pages=1, page_size=2, incremental mode.

    Results arrive newest-first, so advancing the watermark to the global
    newest after a truncated run (and discarding the nextPageToken) skipped
    the 4 unfetched older-but-still-new issues forever. The watermark must
    hold until a run exhausts; truncated runs persist a resume token.
    """
    store.set_meta("backfill_done", "1")
    old_wm = "2024-01-01T00:00:00.000+0000"
    store.set_watermark(old_wm)
    issues = [_issue(i) for i in range(1, 7)]  # updated 2024-01-02 .. 2024-01-07
    client = WatermarkPagingClient(issues, page=2)

    jql = "project = INC ORDER BY updated DESC"
    r1 = ingest.run_sync(store, client, jql, page_size=2, max_pages=1)
    assert store.count() == 2
    # Truncated: the watermark must NOT advance and a resume token must exist.
    assert store.get_watermark() == old_wm
    assert store.get_meta("sync_token")

    r2 = ingest.run_sync(store, client, jql, page_size=2, max_pages=1)
    # The second run resumed from the persisted token, not from page one.
    assert client.calls[1][1] == "2"
    assert store.count() == 4
    assert store.get_watermark() == old_wm

    r3 = ingest.run_sync(store, client, jql, page_size=2, max_pages=1)
    assert store.count() == 6  # every issue ingested despite max_pages=1
    # Exhausted: watermark advances to the global newest across the runs.
    assert store.get_watermark() == "2024-01-07T00:00:00.000+0000"
    assert not store.get_meta("sync_token")
    assert not store.get_meta("sync_pending_newest")
    assert (r1["ingested"], r2["ingested"], r3["ingested"]) == (2, 2, 2)


# ---------------------------------------------------------------------------
# Stale/invalidated resume tokens must not brick the sync
# ---------------------------------------------------------------------------

class _ClientError(Exception):
    """JiraError-shaped (a ``status`` attribute) without importing the client."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class StaleTokenClient:
    """400-rejects any request carrying a page token (as Jira does for expired
    or invalidated nextPageTokens); serves a single page otherwise."""

    def __init__(self, issues):
        self._issues = issues
        self.calls = []  # (jql, next_page_token)

    def search(self, jql, fields=None, max_results=50, next_page_token=None):
        self.calls.append((jql, next_page_token))
        if next_page_token:
            raise _ClientError("Jira returned HTTP 400", status=400)
        return {"issues": self._issues}


def test_stale_backfill_token_is_cleared_and_run_restarts(store, caplog):
    """SCENARIO: a persisted backfill token Jira now 400-rejects. Every run
    used to fail before persisting anything, re-reading the same dead token
    forever; it must instead clear the token and restart from page one."""
    store.set_meta("backfill_token", "EXPIRED-TOKEN")
    store.set_meta("sync_jql", "project = INC")  # unchanged JQL: token kept
    client = StaleTokenClient([SAMPLE_ISSUE])
    with caplog.at_level(logging.WARNING):
        result = ingest.run_sync(store, client, "project = INC")
    assert result["ingested"] == 1
    assert store.count() == 1
    assert client.calls[0][1] == "EXPIRED-TOKEN"  # tried the persisted token
    assert client.calls[1][1] is None             # then restarted from page one
    assert not store.get_meta("backfill_token")
    assert any("resume token" in r.getMessage() for r in caplog.records)


def test_stale_incremental_token_is_cleared_and_run_restarts(store):
    store.set_meta("backfill_done", "1")
    store.set_watermark("2024-01-01T00:00:00.000+0000")
    store.set_meta("sync_token", "EXPIRED-TOKEN")
    store.set_meta("sync_jql", "project = INC")
    client = StaleTokenClient([SAMPLE_ISSUE])
    result = ingest.run_sync(store, client, "project = INC")
    assert result["ingested"] == 1
    assert not store.get_meta("sync_token")


def test_non_4xx_error_on_resume_token_still_propagates(store):
    store.set_meta("backfill_token", "TKN")
    store.set_meta("sync_jql", "project = INC")
    client = FakeClient([], raise_exc=_ClientError("Jira returned HTTP 503", status=503))
    with pytest.raises(_ClientError):
        ingest.run_sync(store, client, "project = INC")


def test_changed_jql_clears_persisted_resume_tokens(store):
    """Editing jira.jql invalidates Jira's query-bound tokens: the next run
    must drop them and paginate from the first page of the new query."""
    store.set_meta("backfill_token", "TOKEN-FOR-OLD-JQL")
    store.set_meta("sync_jql", "project = OLD")
    client = FakeClient([SAMPLE_ISSUE])
    result = ingest.run_sync(store, client, "project = NEW")
    # With the stale token the fake serves an empty page; from page one it
    # serves the issue — so ingested==1 proves the token was dropped.
    assert result["ingested"] == 1
    assert store.get_meta("sync_jql") == "project = NEW"
    assert not store.get_meta("backfill_token")


# ---------------------------------------------------------------------------
# Full-resync support: seen-key tracking + state reset
# ---------------------------------------------------------------------------

def test_run_sync_reports_seen_keys(store):
    result = ingest.run_sync(store, FakeClient([SAMPLE_ISSUE]), "project = INC")
    assert result["seen_keys"] == {"INC-100"}


def test_reset_sync_state_clears_watermark_and_resume_state(store):
    store.set_watermark("2024-01-05T12:00:00Z")
    store.set_meta("backfill_done", "1")
    store.set_meta("backfill_token", "T1")
    store.set_meta("sync_token", "T2")
    store.set_meta("sync_pending_newest", "2024-01-06T00:00:00Z")
    ingest.reset_sync_state(store)
    assert not store.get_watermark()
    for key in ("backfill_done", "backfill_token", "sync_token", "sync_pending_newest"):
        assert not store.get_meta(key)
