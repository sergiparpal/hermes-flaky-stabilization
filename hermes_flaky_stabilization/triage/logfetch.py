"""Log retrieval for hermes-ci-triage.

Turns a ``log_url_or_path`` into raw text, safely, for two input shapes:

* **Local path** — ``realpath``'d, confirmed to be a regular file, read with a
  hard byte cap. No credentials required; this path always works.
* **Remote URL** — HTTPS only, explicit timeout, streamed read with the same
  byte cap, tail-biased: a body over the cap keeps its LAST cap-worth of bytes
  (failures cluster at the end) and reports ``fetch_truncated`` via
  :func:`fetch_with_stats`. The auth header targets the configured CI provider
  (**GitHub Actions**): a ``Bearer`` token read from ``GITHUB_TOKEN`` in the
  environment **at call time** (never at import). A missing/insufficient token
  surfaces as a :class:`LogFetchError` carrying a remediation hint rather than
  a raw traceback.

This module owns *retrieval policy* (provider/token scoping, local-file roots,
byte caps, error mapping). The security-critical transport hardening — HTTPS-
only redirect/connect guards and SSRF address vetting — lives in the lower-level
:mod:`safehttp`, from which :class:`LogFetchError` is re-exported here so callers
keep using ``logfetch.LogFetchError``.

Pure standard library (``os``, ``ssl``, ``socket``, ``urllib``) — no Hermes
dependency, so it unit-tests in isolation.
"""

from __future__ import annotations

import os
import socket
import ssl
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from . import safehttp
from .safehttp import LogFetchError  # re-export: public API + local raises

# --------------------------------------------------------------------------
# Provider configuration — chosen at design time (decision point 1:
# "GitHub Actions"). Local-path retrieval is provider-agnostic and needs none
# of this.
# --------------------------------------------------------------------------

PROVIDER = "github_actions"
TOKEN_ENV_VAR = "GITHUB_TOKEN"

MAX_LOG_BYTES = 25 * 1024 * 1024   # 25 MB hard cap on any retrieved log
DEFAULT_TIMEOUT = 20.0             # seconds, explicit on every network call
_READ_CHUNK = 65_536
_USER_AGENT = "hermes-ci-triage/0.1"

# Remediation hints reused across several LogFetchErrors.
_PERMS_HINT = "Check the path and file permissions."
_TIMEOUT_HINT = "Retry, or point at a smaller/specific job log."

# Optional allowlist of directories local logs may be read from (os.pathsep-
# separated). Set it to confine reads so the tool cannot be steered into
# reading arbitrary files (~/.ssh/id_rsa, .env, …). Precedence: this env var,
# then the unified config's ``triage.log_roots`` (string or list), then no
# restriction (default).
_LOG_ROOTS_ENV = "HERMES_CI_TRIAGE_LOG_ROOTS"


def _config_triage() -> dict:
    """The unified config's ``triage`` section (``{}`` when unavailable).

    Consulted only when the corresponding ``HERMES_CI_TRIAGE_*`` env var is
    unset, so env vars keep highest precedence and env-only setups behave
    exactly as before. Never raises: any config problem degrades to the
    built-in defaults.
    """
    try:
        from .. import config as unified_config

        section = unified_config.load_config().get("triage")
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _env_or_config_list(env_var: str, config_key: str, sep: str) -> list[str]:
    """A list-valued setting from an env var, falling back to the unified config.

    The shape shared by :func:`_token_hosts` and :func:`_log_roots`: the
    ``HERMES_CI_TRIAGE_*`` *env var* wins (highest precedence, split on *sep*);
    only when it is unset/blank is ``triage.<config_key>`` consulted, accepting
    either a *sep*-separated string or an already-split list. Returns the raw
    items in order — per-item stripping, casing, filtering and path resolution
    stay with each caller, which is exactly where the two legitimately differ,
    so extracting only this common core cannot make them drift.

    (``safehttp._allow_private`` is the bool cousin of this idiom but lives one
    layer down and must not import ``logfetch``, so it cannot share this.)
    """
    raw = os.environ.get(env_var, "")
    if raw.strip():
        return raw.split(sep)
    cfg_val = _config_triage().get(config_key)
    if isinstance(cfg_val, str):
        return cfg_val.split(sep)
    if isinstance(cfg_val, list):
        return [str(v) for v in cfg_val]
    return []


def _safe_path(path: str) -> str:
    """Basename only — don't echo the full local path back to the caller."""
    try:
        base = os.path.basename(str(path).rstrip("/").rstrip("\\"))
        return base or "the requested file"
    except Exception:
        return "the requested file"


def is_remote(target: str) -> bool:
    """True when *target* looks like an ``http(s)://`` URL (vs a local path).

    Note: plain ``http://`` is recognised as *remote* here only so it can be
    routed to :func:`fetch_remote`, which then rejects it (HTTPS-only).
    """
    lowered = (target or "").strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def has_remote_credentials() -> bool:
    """True when a remote-fetch token is present.

    Diagnostics only — used to annotate tool output. It is **not** wired to
    the tool's ``check_fn``: a missing token must never hide the tool, because
    local-path triage works without any credentials.
    """
    return bool(os.environ.get(TOKEN_ENV_VAR, "").strip())


# --------------------------------------------------------------------------
# Token scoping (which hosts may receive the auth header)
# --------------------------------------------------------------------------

# The auth token is sent ONLY to these hosts. GitHub Actions job-log URLs live on
# the API host and 302-redirect to a pre-signed blob URL that needs no token, so
# we never have to send it anywhere else. Extend for GitHub Enterprise via
# HERMES_CI_TRIAGE_TOKEN_HOSTS (comma-separated hostnames), or — when that env
# var is unset — the unified config's ``triage.token_hosts`` (string or list).
_DEFAULT_TOKEN_HOSTS = ("api.github.com", "raw.githubusercontent.com")


def _token_hosts() -> set:
    hosts = set(_DEFAULT_TOKEN_HOSTS)
    for h in _env_or_config_list("HERMES_CI_TRIAGE_TOKEN_HOSTS", "token_hosts", ","):
        h = h.strip().lower()
        if h:
            hosts.add(h)
    return hosts


def _host_allows_token(host: str) -> bool:
    """True only for GitHub hosts the token is meant for."""
    host = (host or "").lower()
    return host in _token_hosts() or host.endswith(".githubusercontent.com")


def _timeout_error(url: str, timeout: float) -> LogFetchError:
    """The single source of truth for the fetch-timeout error envelope."""
    return LogFetchError(
        f"Timed out after {timeout}s fetching {safehttp.safe_url(url)}.",
        _TIMEOUT_HINT,
    )


def _read_capped(resp) -> tuple[bytes, bool]:
    """Read a response body, keeping the LAST :data:`MAX_LOG_BYTES` bytes.

    Tail-biased on purpose: the failure signal clusters at the end of a CI
    log, so when the body exceeds the cap the HEAD is discarded (mirroring the
    prefilter's keep-the-tail rule) instead of silently dropping the failure.
    Returns ``(data, truncated)``; memory stays bounded by the cap.
    """
    chunks: deque[bytes] = deque()
    total = 0
    truncated = False
    while True:
        chunk = resp.read(_READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        # Drop whole head chunks while doing so still leaves >= cap bytes.
        while total - len(chunks[0]) >= MAX_LOG_BYTES:
            total -= len(chunks.popleft())
            truncated = True
    data = b"".join(chunks)
    if len(data) > MAX_LOG_BYTES:
        data = data[-MAX_LOG_BYTES:]
        truncated = True
    return data, truncated


# --------------------------------------------------------------------------
# Local-file retrieval
# --------------------------------------------------------------------------

def _log_roots() -> list:
    """Resolved directories that local logs may be read from (opt-in allowlist).

    Precedence: ``HERMES_CI_TRIAGE_LOG_ROOTS`` env var, then the unified
    config's ``triage.log_roots`` (a list, or an ``os.pathsep``-separated
    string), then no restriction.
    """
    roots = []
    for part in _env_or_config_list(_LOG_ROOTS_ENV, "log_roots", os.pathsep):
        part = part.strip()
        if part:
            roots.append(os.path.realpath(os.path.expanduser(part)))
    return roots


def _within_roots(real: str, roots: list) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([real, root]) == root:
                return True
        except ValueError:
            continue  # e.g. different drive on Windows
    return False


def read_local(path: str) -> str:
    """Read a local log file with a realpath check and byte cap.

    When HERMES_CI_TRIAGE_LOG_ROOTS (or the unified config's
    ``triage.log_roots``) is set, the resolved path (after symlink resolution)
    must live inside one of those roots, so the tool cannot be steered into
    reading arbitrary files such as ~/.ssh/id_rsa or .env.
    """
    real = os.path.realpath(os.path.expanduser(path))
    roots = _log_roots()
    if roots and not _within_roots(real, roots):
        raise LogFetchError(
            f"Refusing to read a log outside the allowed roots: {_safe_path(path)}",
            f"The resolved path is outside {_LOG_ROOTS_ENV}; add its directory "
            "to that allowlist or point at an allowed path.",
        )
    p = Path(real)
    if not p.exists():
        raise LogFetchError(
            f"Log file not found: {_safe_path(path)}",
            "Provide a path to an existing CI log file, or an https:// URL "
            "to the raw log.",
        )
    if not p.is_file():
        raise LogFetchError(
            f"Not a regular file: {_safe_path(path)}",
            "Point log_url_or_path at a log file, not a directory, device, "
            "or pipe.",
        )
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise LogFetchError(
            f"Could not stat log file: {_safe_path(path)} ({type(exc).__name__})",
            _PERMS_HINT,
        )
    if size > MAX_LOG_BYTES:
        raise LogFetchError(
            f"Log file too large: {size} bytes (cap {MAX_LOG_BYTES}).",
            "Pre-trim the log, or point at the specific failing job's log.",
        )
    try:
        with p.open("rb") as fh:
            data = fh.read(MAX_LOG_BYTES)
    except OSError as exc:
        raise LogFetchError(
            f"Could not read log file: {_safe_path(path)} ({type(exc).__name__})",
            _PERMS_HINT,
        )
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Remote retrieval
# --------------------------------------------------------------------------

def fetch_remote(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Fetch a remote log over HTTPS with auth, timeout and byte cap.

    Bodies over :data:`MAX_LOG_BYTES` keep the tail (see :func:`_read_capped`);
    use :func:`fetch_with_stats` to learn whether that truncation happened.

    Safety: HTTPS only; the token is attached only to GitHub hosts
    (:func:`_host_allows_token`) and stripped on cross-host redirects; requests
    to loopback/link-local/reserved addresses are refused to blunt SSRF (the
    redirect/connect-time guards live in :mod:`safehttp`).
    """
    return _fetch_remote(url, timeout=timeout)[0]


def _fetch_remote(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[str, bool]:
    """:func:`fetch_remote`, also reporting whether the body was tail-truncated."""
    if not (url or "").strip().lower().startswith("https://"):
        raise LogFetchError(
            # scheme+host only — the full URL may carry a token/id in its query.
            f"Refusing non-HTTPS URL: {safehttp.safe_url(url)}",
            "Use an https:// URL — plain http and other schemes are not "
            "allowed.",
        )

    host = urlsplit(url).hostname or ""
    if safehttp.is_blocked_address(host):
        raise LogFetchError(
            f"Refusing to fetch from a non-routable/internal address: {host}",
            safehttp.SSRF_HINT,
        )

    headers = {"User-Agent": _USER_AGENT, "Accept": "text/plain, */*"}
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if token and _host_allows_token(host):
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers, method="GET")
    context = ssl.create_default_context()
    opener = safehttp.build_opener(context)
    try:
        with opener.open(request, timeout=timeout) as resp:
            data, truncated = _read_capped(resp)
        return data.decode("utf-8", errors="replace"), truncated
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise LogFetchError(
                f"Authentication failed (HTTP {exc.code}) fetching {safehttp.safe_url(url)}.",
                f"Set the {TOKEN_ENV_VAR} environment variable to a token with "
                f"read access to the CI logs, then retry.",
            )
        if exc.code == 404:
            raise LogFetchError(
                f"Log not found (HTTP 404): {safehttp.safe_url(url)}",
                "Check the URL — the run/job log may have expired or rotated.",
            )
        raise LogFetchError(
            f"HTTP error {exc.code} fetching {safehttp.safe_url(url)}.",
            "Verify the URL and your network access.",
        )
    except TimeoutError:
        raise _timeout_error(url, timeout)
    except (urllib.error.URLError, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise _timeout_error(url, timeout)
        raise LogFetchError(
            f"Network error fetching {safehttp.safe_url(url)}: {reason}",
            "Check connectivity and that the host is reachable.",
        )


def fetch(target: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Resolve *target* (local path or https URL) to raw log text."""
    return fetch_with_stats(target, timeout=timeout)[0]


def fetch_with_stats(
    target: str, *, timeout: float = DEFAULT_TIMEOUT
) -> tuple[str, dict[str, object]]:
    """Like :func:`fetch`, but also return retrieval stats.

    ``stats["fetch_truncated"]`` is True when a remote body exceeded
    :data:`MAX_LOG_BYTES` and only the LAST cap-worth of bytes was kept
    (tail-biased — the failure clusters at the end of a CI log).
    :func:`read_local` refuses oversize files outright, so local reads never
    truncate.
    """
    if not target or not str(target).strip():
        raise LogFetchError(
            "Empty log_url_or_path.",
            "Pass a local log file path or an https:// URL to the raw log.",
        )
    target = str(target).strip()
    if is_remote(target):
        text, truncated = _fetch_remote(target, timeout=timeout)
        return text, {"fetch_truncated": truncated}
    return read_local(target), {"fetch_truncated": False}
