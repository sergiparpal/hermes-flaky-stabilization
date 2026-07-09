"""Jira REST client tests with an injected fake transport (no network)."""

import base64
import json

import pytest
from jira_incidents import jira_client as jc


class FakeTransport:
    """Records the last request and returns a canned (status, bytes)."""

    def __init__(self, status=200, payload=None, raise_exc=None):
        self.status = status
        self.payload = payload if payload is not None else {"ok": True}
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        if self.raise_exc:
            raise self.raise_exc
        return self.status, json.dumps(self.payload).encode("utf-8")


def test_basic_auth_header():
    t = FakeTransport()
    client = jc.JiraClient("https://x.atlassian.net", auth_mode="api_token",
                           email="me@corp.com", token="tok123", transport=t)
    client.search("project = INC")
    auth = t.calls[0]["headers"]["Authorization"]
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
    assert decoded == "me@corp.com:tok123"


def test_oauth_bearer_header():
    t = FakeTransport()
    client = jc.JiraClient("https://x.atlassian.net", auth_mode="oauth",
                           token="bearer-tok", transport=t)
    client.get_issue("INC-1")
    assert t.calls[0]["headers"]["Authorization"] == "Bearer bearer-tok"


def test_timeout_is_passed_through():
    t = FakeTransport()
    client = jc.JiraClient("https://x.atlassian.net", token="t", timeout=3.5, transport=t)
    client.search("x")
    assert t.calls[0]["timeout"] == 3.5


def test_search_builds_query_params():
    t = FakeTransport(payload={"issues": [], "nextPageToken": None})
    client = jc.JiraClient("https://x.atlassian.net", token="t", transport=t)
    client.search("project = INC ORDER BY updated DESC", max_results=25, next_page_token="TKN42")
    url = t.calls[0]["url"]
    # Enhanced, non-deprecated search endpoint with token pagination.
    assert "/rest/api/3/search/jql?" in url
    assert "maxResults=25" in url
    assert "nextPageToken=TKN42" in url
    assert "startAt" not in url
    assert "jql=" in url


def test_search_omits_token_on_first_page():
    t = FakeTransport(payload={"issues": []})
    client = jc.JiraClient("https://x.atlassian.net", token="t", transport=t)
    client.search("project = INC")
    assert "nextPageToken" not in t.calls[0]["url"]


def test_get_issue_expands_changelog_by_default():
    t = FakeTransport(payload={"key": "INC-1"})
    client = jc.JiraClient("https://x.atlassian.net", token="t", transport=t)
    client.get_issue("INC-1")
    assert "expand=changelog" in t.calls[0]["url"]


def test_non_2xx_raises_jira_error():
    t = FakeTransport(status=401, payload={"errorMessages": ["unauthorized"]})
    client = jc.JiraClient("https://x.atlassian.net", token="bad", transport=t)
    with pytest.raises(jc.JiraError) as exc:
        client.search("x")
    assert exc.value.status == 401


def test_transport_exception_wrapped():
    t = FakeTransport(raise_exc=jc.JiraError("network error: timeout"))
    client = jc.JiraClient("https://x.atlassian.net", token="t", transport=t)
    with pytest.raises(jc.JiraError):
        client.search("x")


def test_base_url_required():
    with pytest.raises(ValueError):
        jc.JiraClient("", token="t")


# ---------------------------------------------------------------------------
# Security: refuse cleartext credential transport; don't leak response bodies
# (audit findings 2 + 7).
# ---------------------------------------------------------------------------

def test_https_required_rejects_http():
    with pytest.raises(ValueError):
        jc.JiraClient("http://x.atlassian.net", token="t")


def test_https_required_rejects_other_schemes():
    for url in ("ftp://x", "file:///etc/passwd", "x.atlassian.net"):
        with pytest.raises(ValueError):
            jc.JiraClient(url, token="t")


def test_error_message_excludes_response_body():
    # A 4xx/5xx body can echo incident text and is logged by the sync thread;
    # the exception must carry the status only, not the raw body.
    t = FakeTransport(status=403, payload={"errorMessages": ["SENSITIVE-BODY-XYZ"]})
    client = jc.JiraClient("https://x.atlassian.net", token="t", transport=t)
    with pytest.raises(jc.JiraError) as exc:
        client.search("x")
    assert exc.value.status == 403
    assert "SENSITIVE-BODY-XYZ" not in str(exc.value)
