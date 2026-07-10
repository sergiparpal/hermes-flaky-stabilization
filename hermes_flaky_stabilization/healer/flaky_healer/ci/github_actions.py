"""GitHub Actions adapter: run metadata, jobs, and the run-logs zip."""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlparse

from .base import MAX_RESPONSE_BYTES, CIError, Transport, UrllibTransport

API_VERSION = "2022-11-28"
USER_AGENT = "hermes-flaky-healer/0.1.0"

# Hard cap for the run-logs zip. The default UrllibTransport already enforces
# this while streaming; the check in download_logs_zip keeps the guarantee for
# injected transports too (the zipsafe budget only applies AFTER download).
MAX_LOGS_ZIP_BYTES = MAX_RESPONSE_BYTES

# repo/run_id are interpolated into the request path; constrain them to their
# real shapes so a crafted value cannot traverse to another API path or inject a
# query/fragment, and percent-encode what survives as belt-and-suspenders.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_RUN_ID_RE = re.compile(r"^[0-9]+$")


def _safe_repo(repo: str) -> str:
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise CIError(f"invalid repo {repo!r}: expected 'owner/name'")
    owner, name = repo.split("/", 1)
    if owner in (".", "..") or name in (".", ".."):
        raise CIError(f"invalid repo {repo!r}: path-traversal segment not allowed")
    return quote(repo, safe="/")


def _safe_run_id(run_id) -> str:
    run_id = str(run_id)
    if not _RUN_ID_RE.match(run_id):
        raise CIError(f"invalid run id {run_id!r}: expected a numeric GitHub Actions run id")
    return quote(run_id, safe="")


class GitHubActionsCI:
    def __init__(
        self,
        token: str,
        transport: Transport | None = None,
        api_base: str = "https://api.github.com",
    ):
        self._token = token
        self._transport = transport or UrllibTransport()
        base = api_base.rstrip("/")
        # Reject non-http(s) bases: the bearer token rides on this URL, so a
        # file://, gopher:// etc. base must never receive it.
        if urlparse(base).scheme.lower() not in ("http", "https"):
            raise CIError(f"unsupported API base {api_base!r}: expected an http(s) URL")
        self._base = base

    def _headers(self) -> dict:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _get(self, url: str) -> bytes:
        try:
            status, _headers, body = self._transport.request("GET", url, headers=self._headers())
        except OSError as exc:  # URLError, socket timeout, SSL, connection reset all subclass this
            raise CIError(f"GET {url} failed: {exc}") from exc
        if status >= 400:
            snippet = body[:200].decode("utf-8", "replace")
            raise CIError(f"GET {url} -> HTTP {status}: {snippet}", status=status)
        return body

    def _get_json(self, url: str) -> dict:
        try:
            return json.loads(self._get(url).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CIError(f"GET {url} returned non-JSON payload: {exc}") from exc

    def get_run(self, repo: str, run_id: str) -> dict:
        repo, run_id = _safe_repo(repo), _safe_run_id(run_id)
        return self._get_json(f"{self._base}/repos/{repo}/actions/runs/{run_id}")

    def get_jobs(self, repo: str, run_id: str) -> dict:
        """All jobs for a run, following pagination (matrix builds exceed 100)."""
        repo, run_id = _safe_repo(repo), _safe_run_id(run_id)
        jobs: list = []
        total_count = 0
        for page in range(1, 51):  # hard cap: 50 pages = 5000 jobs
            data = self._get_json(
                f"{self._base}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}"
            )
            batch = data.get("jobs") or []
            jobs.extend(batch)
            total_count = data.get("total_count") or len(jobs)
            if len(batch) < 100 or len(jobs) >= total_count:
                break
        return {"total_count": total_count, "jobs": jobs}

    def download_logs_zip(self, repo: str, run_id: str) -> bytes:
        # GitHub answers with a 302 to short-lived blob storage; urllib follows it.
        repo, run_id = _safe_repo(repo), _safe_run_id(run_id)
        body = self._get(f"{self._base}/repos/{repo}/actions/runs/{run_id}/logs")
        if len(body) > MAX_LOGS_ZIP_BYTES:
            raise CIError(
                f"run-logs zip for {repo} run {run_id} exceeds the "
                f"{MAX_LOGS_ZIP_BYTES}-byte cap"
            )
        return body
