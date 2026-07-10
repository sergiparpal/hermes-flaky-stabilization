"""Credential masking for anything the healer writes to the audit log.

A leaf module: it imports nothing but the standard library, so both
``healer.handlers`` and ``flaky_healer.recipes.store`` can depend on it. That
is the point — ``handlers`` imports ``recipes.store``, so the two cannot import
each other, and the token pattern used to be *copy-pasted* between them with a
comment noting the cycle. A credential format added to one copy and not the
other would have leaked through the other path.

Two complementary rules, applied together by :func:`redact`:

* by key name  — a key that looks like a credential masks its whole value;
* by value shape — a token-shaped substring is masked wherever it appears,
  including under an innocent key or embedded in free-form text (e.g. a token
  pasted into a dispatched ``command``), which the key-name check alone misses.
"""

from __future__ import annotations

import re

MASK = "***"

# Cap on the repr() audit fallback: enough to identify the object, bounded so a
# huge non-serializable payload cannot bloat the audit table.
AUDIT_REPR_LIMIT = 2000

SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|access[_-]?key|bearer)", re.I
)

TOKEN_VALUE_RE = re.compile(
    r"(?:gh[poursa]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"
    r"|[Bb]earer\s+[A-Za-z0-9._\-]{12,})"
)


def mask_tokens(text: str) -> str:
    """Mask every token-shaped substring in *text*."""
    return TOKEN_VALUE_RE.sub(MASK, text)


def redact(obj):
    """Recursively mask credentials in *obj* by key name and by value shape."""
    if isinstance(obj, dict):
        return {
            k: (MASK if isinstance(k, str) and SENSITIVE_KEY_RE.search(k) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list | tuple):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return mask_tokens(obj)
    return obj


def repr_fallback(payload) -> dict[str, str]:
    """The audit payload for an object ``json.dumps`` refused to encode.

    Objects that survive :func:`redact` untouched may still embed credentials in
    their ``repr``, so mask token-shaped values once more before storing. Both
    the handler (before the store call) and the store (as a last line of
    defence) route through here.
    """
    return {"repr": mask_tokens(repr(payload))[:AUDIT_REPR_LIMIT]}
