"""Failure-signature normalization and fuzzy tokenization (pure functions).

A **signature** is a SHA-1 of a failure excerpt after volatile tokens
(timestamps, hex addresses, UUIDs, line/byte offsets, temp paths, bare numbers)
are normalised away, so two runs of the same failure that differ only in those
tokens collapse to one signature. The fuzzy helpers pick salient tokens for the
FTS "similar prior" lookup.

This is deliberately separate from :mod:`patterns` (the SQLite persistence
class): the normalization rules and the fuzzy ranking change for *correctness*
reasons, the store for *persistence* reasons. Keeping them apart lets the
signature logic be read and unit-tested without dragging SQLite along. Pure
standard library (``re``, ``hashlib``, ``sqlite3`` only to probe FTS5 support).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

# ORDER MATTERS: rules run top-to-bottom, so specific patterns must precede
# general ones (timestamps before clock times; long hex blobs before the
# bare-integer catch-all). Reordering can let a greedy rule mask a precise one.
_NORMALISERS = [
    # ISO-8601-ish timestamps: 2026-05-31T17:04:01.123Z / 2026-05-31 17:04:01
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    # clock times: 17:04:01 / 17:04:01.123
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<TS>"),
    # UUIDs
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    # hex addresses: 0xdeadbeef
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<ADDR>"),
    # long hex blobs (git sha, content hashes) — 7+ hex chars
    (re.compile(r"\b[0-9a-fA-F]{7,}\b"), "<HEX>"),
    # temp paths
    (re.compile(r"(?:/tmp|/var/folders|/var/tmp|/private/var/folders)/[^\s:'\"]+"), "<TMP>"),
    # explicit line/offset markers: 'line 1234', 'offset 42', ':123:45'
    (re.compile(r"\b(?:line|offset|byte)\s+\d+\b", re.IGNORECASE), r"<POS>"),
    (re.compile(r":\d+:\d+\b"), ":<POS>"),
    (re.compile(r":\d+\b"), ":<POS>"),
    # any remaining bare integer run
    (re.compile(r"\b\d+\b"), "<N>"),
]


def normalize_signature_text(text: str) -> str:
    """Normalise volatile tokens so equivalent failures share a signature."""
    out = text or ""
    for pattern, repl in _NORMALISERS:
        out = pattern.sub(repl, out)
    # collapse whitespace so reflowed/indented variants converge
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()


def compute_signature(excerpt: str) -> str | None:
    """Return a stable SHA-1 hex signature for a (pre-filtered) excerpt.

    Returns ``None`` for a *degenerate* excerpt — one whose volatile tokens
    normalise away to nothing (blank/whitespace-only, or pure timestamps/IDs).
    Such an excerpt hashes to the empty-string signature, which would collide
    across every such log, so it must not be stored or looked up. Returning
    ``None`` keeps that rule with the signature logic instead of forcing the
    caller to re-derive it by testing the normalised text for emptiness.
    """
    normalised = normalize_signature_text(excerpt)
    if not normalised:
        return None
    # Non-cryptographic content fingerprint (dedup key), hence usedforsecurity=False.
    return hashlib.sha1(
        normalised.encode("utf-8", "replace"), usedforsecurity=False
    ).hexdigest()


def fts5_available() -> bool:
    """Probe whether the bundled sqlite3 supports FTS5 (no side effects)."""
    try:
        conn = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(c)")
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


_FTS_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

# Ubiquitous failure vocabulary — present in virtually every CI log, so a fuzzy
# match on one of these alone is meaningless (it would surface the project's
# most-seen pattern as a "similar" prior for any unrelated failure). Excluded
# from the fuzzy query and from the overlap count.
_FUZZY_STOPWORDS = frozenset({
    "error", "errors", "errored",
    "fail", "fails", "failed", "failing", "failure", "failures",
    "exception", "exceptions",
})

# How many fuzzy candidates to inspect for genuine token overlap.
FUZZY_CANDIDATES = 10


def fuzzy_tokens(excerpt: str, limit: int = 12) -> list[str]:
    """Pick salient alphanumeric tokens from an excerpt for an FTS query.

    Stopwords (ubiquitous failure vocabulary) are excluded: they carry no
    similarity signal and would let any log fuzzy-match any pattern.
    """
    seen: dict[str, None] = {}
    for tok in _FTS_TOKEN_RE.findall(normalize_signature_text(excerpt)):
        low = tok.lower()
        if low in _FUZZY_STOPWORDS:
            continue
        if low not in seen:
            seen[low] = None
        if len(seen) >= limit:
            break
    return list(seen)


def sample_tokens(sample: str) -> set[str]:
    """The stored sample's token set, derived the same way as the query tokens."""
    return {t.lower() for t in _FTS_TOKEN_RE.findall(normalize_signature_text(sample or ""))}
