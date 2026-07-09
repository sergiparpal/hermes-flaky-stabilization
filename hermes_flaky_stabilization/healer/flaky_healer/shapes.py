"""Shape normalization shared by diagnosis and recipe signatures.

Strips volatile fragments (hex ids, numbers, durations) so two occurrences of
the same failure normalize to identical text: ``#btn-1f9c`` → ``#btn-<h>``.
"""

from __future__ import annotations

import re

# hex-ish token of ≥3 chars containing BOTH a hex letter and a digit ("1f9c",
# "a1b2c3"). Pure-digit runs ("2000", "99", "100") are left for _NUM so they
# normalize to <n> consistently regardless of length (no 2-vs-3-digit split).
_HEX_TOKEN = re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{3,}\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def shape(text: str | None) -> str:
    if not text:
        return ""
    out = _HEX_TOKEN.sub("<h>", text)
    out = _NUM.sub("<n>", out)
    return _WS.sub(" ", out).strip()


def selector_shape(selector: str | None) -> str:
    return shape(selector)


def message_shape(message: str | None, max_len: int = 200) -> str:
    if not message:
        return ""
    stripped = message.strip()
    if not stripped:  # whitespace/ANSI-only message: splitlines() would be empty
        return ""
    return shape(stripped.splitlines()[0])[:max_len]
