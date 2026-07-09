"""'No PII leaks' sweep (plan §5 gate): seed incidents with known PII, push
them through every model-facing path, and assert none of the seeded PII
strings survive."""


import pytest
from jira_incidents import IncidentsService, ingest

# Seeded PII tokens that must NEVER appear in model-facing output.
SEEDED_PII = [
    "jane.doe@corp.com",          # email
    "ops-oncall@corp.com",        # email
    "415-555-0132",               # phone
    "+1 408 555 0199",            # intl phone
    "192.168.50.7",               # internal IP
    "ATATT3xFfQbADs12345_-=secret",  # Atlassian token
    "Jane Doe",                   # reporter name
    "Bob Roe",                    # assignee name
]

# A Jira issue whose every text field is contaminated with PII.
PII_ISSUE = {
    "key": "INC-777",
    "fields": {
        "summary": "Outage reported by jane.doe@corp.com, paged Jane Doe",
        "status": {"name": "Resolved"},
        "reporter": {"displayName": "Jane Doe"},
        "assignee": {"displayName": "Bob Roe"},
        "created": "2024-02-01T00:00:00.000+0000",
        "updated": "2024-02-02T00:00:00.000+0000",
        "description": {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text":
                "Host 192.168.50.7 down. Call +1 408 555 0199 or 415-555-0132. "
                "Token ATATT3xFfQbADs12345_-=secret. Contact ops-oncall@corp.com."}]}],
        },
    },
}


class FakeClient:
    def search(self, jql, fields=None, max_results=50, next_page_token=None):
        if next_page_token:
            return {"issues": []}
        return {"issues": [PII_ISSUE]}  # single page, no nextPageToken


@pytest.fixture
def provider(tmp_home):
    p = IncidentsService(config={"jira_base_url": "https://x.atlassian.net"})
    p.initialize(session_id="pii", hermes_home=tmp_home, platform="cli")
    # Ingest the contaminated issue directly into the local store.
    ingest.run_sync(p._store, FakeClient(), "project = INC")
    yield p
    p.shutdown()


def _assert_clean(text: str, where: str):
    for pii in SEEDED_PII:
        assert pii not in text, f"PII '{pii}' leaked via {where}: {text!r}"


def test_store_actually_indexed_the_issue(provider):
    assert provider._store.get("INC-777") is not None


def test_no_leak_in_llm_context(provider):
    # Replaces the dropped system_prompt_block: the pre_llm_call injection is
    # now the automatic model-facing path (plan D1).
    out = provider.llm_context("outage host token contact incident")
    _assert_clean(out["context"] if out else "", "llm_context")


def test_no_leak_in_prefetch(provider):
    out = provider.prefetch("outage host token contact incident")
    assert "INC-777" in out  # it DID surface the incident…
    _assert_clean(out, "prefetch")          # …but with PII stripped


def test_no_leak_in_search_tool(provider):
    out = provider.handle_tool_call("jira_search_incident", {"query": "outage host token"})
    _assert_clean(out, "jira_search_incident")


def test_no_leak_in_root_cause_tool(provider):
    out = provider.handle_tool_call("jira_get_root_cause", {"incident_key": "INC-777"})
    _assert_clean(out, "jira_get_root_cause")


def test_no_leak_in_link_tool(provider):
    # Even user-supplied note PII is scrubbed on the echoed result.
    out = provider.handle_tool_call(
        "jira_link_session",
        {"incident_key": "INC-777", "note": "reported by jane.doe@corp.com"},
    )
    _assert_clean(out, "jira_link_session")


def test_raw_store_still_has_full_data(provider):
    """Redaction is on egress only — the local index keeps the full record."""
    row = provider._store.get("INC-777")
    assert "192.168.50.7" in row["body"]  # raw data retained locally
