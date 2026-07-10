"""Transport and CI adapter protocols + the stdlib urllib transport."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable


class CIError(RuntimeError):
    """HTTP/protocol failure talking to a CI provider."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# Hard cap on a single HTTP response body. Run-logs zips realistically reach
# hundreds of MB, and an unbounded ``resp.read()`` buffers the whole payload in
# memory BEFORE the zipsafe budget can apply. 100 MiB is comfortably above any
# legitimate payload this plugin processes while bounding worst-case memory.
MAX_RESPONSE_BYTES = 100 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


def _read_capped(resp, url: str) -> bytes:
    """Read ``resp`` in chunks, raising ``CIError`` beyond MAX_RESPONSE_BYTES."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise CIError(
                f"response for {url} exceeds the {MAX_RESPONSE_BYTES}-byte cap; "
                "refusing to buffer it"
            )
        chunks.append(chunk)


@runtime_checkable
class Transport(Protocol):
    """Minimal HTTP surface so tests can inject a fake (plan Phase 1.3)."""

    def request(
        self, method: str, url: str, headers: dict | None = None, body: bytes | None = None,
        timeout: int = 30,
    ) -> tuple[int, dict, bytes]: ...


class UrllibTransport:
    """Default stdlib transport. Follows redirects (GitHub log downloads 302).

    The ``Authorization`` header is attached as an *unredirected* header so the
    stdlib redirect handler does NOT replay it to the cross-host target of the
    run-logs 302 (short-lived blob storage), which would leak the token.
    """

    def request(self, method, url, headers=None, body=None, timeout=30):
        req = urllib.request.Request(url, data=body, method=method.upper())
        for key, value in dict(headers or {}).items():
            if key.lower() == "authorization":
                req.add_unredirected_header(key, value)
            else:
                req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.status, dict(resp.headers), _read_capped(resp, url)
        except urllib.error.HTTPError as exc:
            # Error bodies only feed short diagnostic snippets; one chunk is plenty.
            return exc.code, dict(exc.headers or {}), exc.read(_READ_CHUNK_BYTES) or b""


@runtime_checkable
class CIAdapter(Protocol):
    """Provider-neutral surface the rest of the plugin codes against."""

    def get_run(self, repo: str, run_id: str) -> dict: ...

    def get_jobs(self, repo: str, run_id: str) -> dict: ...

    def download_logs_zip(self, repo: str, run_id: str) -> bytes: ...
