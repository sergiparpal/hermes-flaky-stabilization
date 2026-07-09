"""GitHub Actions adapter tests over the recorded fixtures (offline)."""

import json

import pytest
from conftest import FIXTURES, FakeTransport
from flaky_healer.ci.base import CIError
from flaky_healer.ci.github_actions import API_VERSION, GitHubActionsCI

REPO = "acme/webshop"
RUN_ID = "9876543210"
BASE = "https://api.github.com"


@pytest.fixture
def transport():
    t = FakeTransport()
    t.add(
        "GET",
        f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}",
        json.loads((FIXTURES / "gh" / "run.json").read_text()),
    )
    t.add(
        "GET",
        f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}/jobs?per_page=100&page=1",
        json.loads((FIXTURES / "gh" / "jobs.json").read_text()),
    )
    t.add(
        "GET",
        f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}/logs",
        (FIXTURES / "gh" / "run_logs.zip").read_bytes(),
    )
    return t


def test_get_run_parses_fixture(transport):
    ci = GitHubActionsCI(token="tok", transport=transport)
    run = ci.get_run(REPO, RUN_ID)
    assert run["conclusion"] == "failure"
    assert run["id"] == 9876543210


def test_sends_auth_and_api_version_headers(transport):
    ci = GitHubActionsCI(token="secret-token", transport=transport)
    ci.get_run(REPO, RUN_ID)
    headers = transport.requests[0]["headers"]
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["X-GitHub-Api-Version"] == API_VERSION
    assert headers["Accept"] == "application/vnd.github+json"
    assert "hermes-flaky-healer" in headers["User-Agent"]


def test_get_jobs_returns_failed_job(transport):
    ci = GitHubActionsCI(token="tok", transport=transport)
    jobs = ci.get_jobs(REPO, RUN_ID)
    failed = [j for j in jobs["jobs"] if j["conclusion"] == "failure"]
    assert [j["name"] for j in failed] == ["e2e-tests"]


def test_download_logs_zip_returns_bytes(transport):
    ci = GitHubActionsCI(token="tok", transport=transport)
    blob = ci.download_logs_zip(REPO, RUN_ID)
    assert blob[:2] == b"PK"  # zip magic


def test_http_error_raises_cierror(transport):
    ci = GitHubActionsCI(token="tok", transport=transport)
    with pytest.raises(CIError) as exc_info:
        ci.get_run(REPO, "0000")
    assert exc_info.value.status == 404


def test_api_base_override():
    t = FakeTransport()
    t.add("GET", f"https://ghe.corp.local/api/v3/repos/{REPO}/actions/runs/{RUN_ID}", {"id": 1})
    ci = GitHubActionsCI(token="tok", transport=t, api_base="https://ghe.corp.local/api/v3/")
    assert ci.get_run(REPO, RUN_ID) == {"id": 1}


def test_non_json_payload_raises_cierror():
    t = FakeTransport()
    t.add("GET", f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}", b"\x89PNG not json")
    ci = GitHubActionsCI(token="tok", transport=t)
    with pytest.raises(CIError):
        ci.get_run(REPO, RUN_ID)
