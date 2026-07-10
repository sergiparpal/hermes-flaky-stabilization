"""Credential masking for anything the healer writes to the audit log.

Two complementary rules, applied together by :func:`redact`:

* by key name  — a key that looks like a credential masks its whole value;
* by value shape — a token-shaped substring is masked wherever it appears,
  including under an innocent key or embedded in free-form text (e.g. a token
  pasted into a dispatched ``command``), which the key-name check alone misses.

The credential *shapes* (``TOKEN_VALUE_RE``) and the sensitive-key matcher live
once in :mod:`hermes_flaky_stabilization.common.secretscrub`, shared with the
triage log scrubber and the incidents PII redactor — the three used to keep
divergent copies, so a format fixed in one leaked through the others. This
module keeps only the healer conventions: the ``***`` mask, the recursive walk,
and the audit ``repr`` fallback. Still a leaf for the intra-healer graph
(``handlers`` imports ``recipes.store``, so the two must not import each other,
and ``secretscrub`` imports nothing from the plugin — no cycle).
"""

from __future__ import annotations

# Absolute import (not relative): the healer core is loaded both as the unified
# ``hermes_flaky_stabilization.healer.flaky_healer`` package and, via the stage
# shim, as a flat ``flaky_healer`` — an absolute import resolves in both, the
# same pattern ``config.py`` uses for the unified config. ``secretscrub`` is
# stdlib-only and imports nothing from the plugin, so there is no cycle.
from hermes_flaky_stabilization.common import secretscrub

MASK = "***"

# Cap on the repr() audit fallback: enough to identify the object, bounded so a
# huge non-serializable payload cannot bloat the audit table.
AUDIT_REPR_LIMIT = 2000

# Shared with the triage scrubber and the PII redactor (one owner of the shapes).
SENSITIVE_KEY_RE = secretscrub.SENSITIVE_KEY_RE
TOKEN_VALUE_RE = secretscrub.TOKEN_VALUE_RE


def mask_tokens(text: str) -> str:
    """Mask every token-shaped substring in *text*."""
    return secretscrub.mask_token_shapes(text, MASK)


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
