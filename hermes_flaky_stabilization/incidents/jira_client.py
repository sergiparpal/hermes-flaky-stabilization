"""Minimal Jira REST client (standard library only).

Wraps the few Jira Cloud REST endpoints the ingester needs:

* ``search(jql, fields, max_results, next_page_token)`` — token-paginated issue
  search via the enhanced ``/search/jql`` endpoint (replaces the deprecated
  ``/search`` + ``startAt`` paging).
* ``get_issue(key, fields, expand)`` — single issue (optionally w/ changelog)

Design
------
* No third-party dependencies — uses :mod:`urllib`.
* Connect/read timeouts are always enforced (plan §3 / §5.1).
* The HTTP layer is injectable: pass ``transport=callable(method, url,
  headers, body, timeout) -> (status, bytes)`` to mock it in tests with no
  network.  The default transport uses ``urllib.request``.
* Auth supports two modes:
    - ``api_token`` — HTTP Basic with ``email:api_token`` (Jira Cloud).
    - ``oauth``     — ``Authorization: Bearer <token>``.
* The token is only ever read from the caller; it is never logged or stored.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from ..common import nethttp

# A transport takes (method, url, headers, body_bytes_or_None, timeout) and
# returns (status_code, response_bytes).
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]

DEFAULT_TIMEOUT = 10.0
DEFAULT_SEARCH_FIELDS = ["summary", "status", "reporter", "assignee", "created", "updated", "description"]

# Explicit, verifying TLS context (cert chain + hostname). urllib already uses
# a verifying context for HTTPS by default; pinning it here makes the security
# posture auditable and independent of interpreter/global-default changes.
_SSL_CONTEXT = ssl.create_default_context()


class JiraError(Exception):
    """Raised on transport failures or non-2xx Jira responses."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _urllib_transport(method: str, url: str, headers: dict[str, str],
                      body: bytes | None, timeout: float) -> tuple[int, bytes]:
    # Build via the shared helper so the Basic/Bearer credential is attached
    # UNREDIRECTED — urllib must never replay it to a redirect target (a proxy
    # bounce or http→https upgrade would otherwise leak it in cleartext).
    req = nethttp.build_request(method, url, headers, body)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        # HTTPError is also a response — surface status + body for context.
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return e.code, payload
    except urllib.error.URLError as e:
        raise JiraError(f"network error: {e.reason}") from e
    except Exception as e:  # socket.timeout etc.
        raise JiraError(f"request failed: {e}") from e


class JiraClient:
    def __init__(
        self,
        base_url: str,
        auth_mode: str = "api_token",
        email: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        api_version: str = "3",
        transport: Transport | None = None,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        # Refuse to send credentials over a cleartext / non-HTTPS scheme. Basic
        # auth carries base64(email:token); on http:// that token is trivially
        # sniffable. https:// also pins us to a verifying TLS context above.
        scheme = urllib.parse.urlsplit(self.base_url).scheme.lower()
        if scheme != "https":
            raise ValueError(
                "jira_base_url must use https:// — refusing to send Jira "
                f"credentials over an insecure scheme ({scheme or 'none'!r})"
            )
        self.auth_mode = (auth_mode or "api_token").lower()
        self.email = email
        self._token = token
        self.timeout = float(timeout) if timeout else DEFAULT_TIMEOUT
        self.api_version = str(api_version)
        self._transport: Transport = transport or _urllib_transport

    # -- auth ----------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if not self._token:
            return headers
        if self.auth_mode == "oauth":
            headers["Authorization"] = f"Bearer {self._token}"
        else:  # api_token (Basic)
            raw = f"{self.email or ''}:{self._token}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    # -- request plumbing ----------------------------------------------------

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = self._auth_headers()
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, payload = self._transport(method, url, headers, data, self.timeout)
        if not (200 <= status < 300):
            # Surface the status code only — never the raw response body, which
            # can echo incident text and lands in logs via the sync thread.
            raise JiraError(f"Jira returned HTTP {status}", status=status)
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception as e:
            raise JiraError(f"invalid JSON from Jira: {e}", status=status) from e

    # -- endpoints -----------------------------------------------------------

    def search(self, jql: str, fields: list[str] | None = None,
               max_results: int = 50, next_page_token: str | None = None,
               expand: str | None = None) -> dict[str, Any]:
        """Search issues by JQL via the enhanced ``/search/jql`` endpoint.

        Returns the raw Jira search response dict, which includes the next
        page's ``nextPageToken`` (absent on the last page). This replaces the
        deprecated ``/search`` endpoint and its ``startAt``/``total`` paging
        with token-based pagination (Jira Cloud platform REST v3).
        """
        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": int(max_results),
            "fields": ",".join(fields or DEFAULT_SEARCH_FIELDS),
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token
        if expand:
            params["expand"] = expand
        return self._request("GET", f"/rest/api/{self.api_version}/search/jql", params=params)

    def get_issue(self, key: str, fields: list[str] | None = None,
                  expand: str | None = "changelog") -> dict[str, Any]:
        """Fetch one issue, expanding the changelog by default.

        Part of the client's public surface (covered by tests) for single-issue
        lookups; the incremental sync path uses :meth:`search` exclusively, so
        this is not on the ingest hot path.
        """
        if not key:
            raise ValueError("issue key is required")
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = expand
        return self._request(
            "GET", f"/rest/api/{self.api_version}/issue/{urllib.parse.quote(key)}",
            params=params or None,
        )

    # -- NET-NEW (unified plugin, plan D7): the single write endpoint ---------

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        """POST ``/rest/api/{v}/issue`` — create one issue from *fields*.

        The legacy client was deliberately GET-only; this is the one write the
        unified plugin adds (``jira_create_incident``, plan D7). It reuses the
        hardened transport: https-only base URL, enforced timeouts, and error
        surfaces that carry the HTTP status but NEVER the response body (which
        could echo the request's incident text into logs).
        """
        if not isinstance(fields, dict) or not fields:
            raise ValueError("fields must be a non-empty dict")
        return self._request(
            "POST", f"/rest/api/{self.api_version}/issue", body={"fields": fields}
        )
