"""Ingest mapping + incremental sync tests (plan §3). No network."""

import os

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
