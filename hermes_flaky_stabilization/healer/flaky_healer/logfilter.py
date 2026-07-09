"""Local pre-filter for raw CI logs (plan Phase 1.4).

Raw multi-MB logs must never be returned to the model: scan for failure
anchors, keep ±`context` lines around each, merge overlapping windows, drop
duplicate blocks (numbers/hashes normalized first, so retry spam collapses)
and cap the result at ~15 KB.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

DEFAULT_CONTEXT = 40
DEFAULT_CAP_BYTES = 15 * 1024
_TAIL_LINES = 60

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ?")

_ANCHOR_RE = re.compile(
    r"(\bFAIL(?:ED|URE)?\b"
    r"|\b[A-Z][A-Za-z]+Error\b"  # TimeoutError, ReferenceError, AssertionError, …
    r"|\bError\b"
    r"|Timeout \d+ms"
    r"|✘|✗|×"
    r"|\bTraceback\b"
    r"|exit code [1-9]\d*"
    r"|##\[error\]"
    r"|\b[1-9]\d* failed\b"
    r"|\bexpect\()"
)

# Normalization for duplicate detection only: numbers, hex ids and durations
# vary between retries of the same failure.
_NUM_RE = re.compile(r"\d+")
_HEX_RE = re.compile(r"\b[0-9a-f]{6,}\b", re.IGNORECASE)


@dataclass
class FilteredLog:
    text: str
    bytes_raw: int
    bytes_filtered: int
    anchor_count: int
    blocks_kept: int
    blocks_dropped_duplicate: int
    blocks_dropped_cap: int
    note: str = ""


def _clean_line(line: str) -> str:
    # Every line of a multi-MB log passes through here; the ANSI scan is the
    # expensive part, so skip it on the (common) lines with no ESC byte.
    if "\x1b" in line:
        line = _ANSI_RE.sub("", line)
    return _TS_RE.sub("", line).rstrip()


def _block_digest(block: list[str]) -> str:
    normalized = "\n".join(
        _NUM_RE.sub("<n>", _HEX_RE.sub("<h>", ln)).strip() for ln in block
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def filter_log(
    raw: str,
    *,
    context: int = DEFAULT_CONTEXT,
    cap_bytes: int = DEFAULT_CAP_BYTES,
) -> FilteredLog:
    bytes_raw = len(raw.encode("utf-8", "replace"))
    lines = [_clean_line(ln) for ln in raw.splitlines()]

    anchor_idx = [i for i, ln in enumerate(lines) if _ANCHOR_RE.search(ln)]
    if not anchor_idx:
        tail = "\n".join(lines[-_TAIL_LINES:]).encode()[-4096:].decode("utf-8", "replace")
        return FilteredLog(
            text=tail,
            bytes_raw=bytes_raw,
            bytes_filtered=len(tail.encode()),
            anchor_count=0,
            blocks_kept=1 if tail else 0,
            blocks_dropped_duplicate=0,
            blocks_dropped_cap=0,
            note="no failure anchors found; returning log tail",
        )

    # Merge overlapping ±context windows into blocks.
    windows: list[list[int]] = []
    for i in anchor_idx:
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])

    pieces: list[str] = []
    seen: set[str] = set()
    used = 0
    kept = dropped_dup = dropped_cap = 0
    for lo, hi in windows:
        block = lines[lo:hi]
        digest = _block_digest(block)
        if digest in seen:
            dropped_dup += 1
            continue
        seen.add(digest)
        body = f"[lines {lo + 1}-{hi}]\n" + "\n".join(block)
        size = len(body.encode()) + 2
        if used + size > cap_bytes:
            # A single merged window can exceed the cap on its own (an
            # anchor-dense failure region). Keep its head rather than drop it
            # whole — that is exactly where the failure evidence lives.
            remaining = cap_bytes - used - 2
            if remaining > 512 and kept == 0:
                clipped = body.encode("utf-8")[:remaining].decode("utf-8", "ignore")
                pieces.append(clipped + "\n[... block truncated to fit size cap ...]")
                used = cap_bytes
                kept += 1
            dropped_cap += 1
            continue
        pieces.append(body)
        used += size
        kept += 1

    note = ""
    if dropped_cap:
        note = f"{dropped_cap} additional failure block(s) omitted to stay under the size cap"
        pieces.append(f"[... {note} ...]")
    text = "\n\n".join(pieces)
    return FilteredLog(
        text=text,
        bytes_raw=bytes_raw,
        bytes_filtered=len(text.encode()),
        anchor_count=len(anchor_idx),
        blocks_kept=kept,
        blocks_dropped_duplicate=dropped_dup,
        blocks_dropped_cap=dropped_cap,
        note=note,
    )
