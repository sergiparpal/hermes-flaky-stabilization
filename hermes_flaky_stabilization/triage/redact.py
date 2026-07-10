"""Secret redaction for log excerpts before they leave the host.

CI logs routinely contain credentials (tokens, keys, connection strings). This
module scrubs the common formats from any text that is about to be (a) sent to
the LLM provider for classification, (b) forwarded to another tool, or (c)
echoed back in the tool's result — so a secret that leaked into a build log is
not propagated or reflected further.

The secret *shapes* live once in :mod:`hermes_flaky_stabilization.common.secretscrub`
(shared with the healer's audit masking and the incidents PII redactor, so a
new credential format is added in one place). This module owns only the triage
convention: replace every match with :data:`PLACEHOLDER`. Conservative by
design — it targets well-known secret shapes and explicit ``key = value`` secret
assignments, so it does not mangle ordinary failure text.
"""

from __future__ import annotations

from typing import Any

from ..common import secretscrub

PLACEHOLDER = secretscrub.DEFAULT_PLACEHOLDER  # "<REDACTED>"


def redact(text: str) -> str:
    """Return *text* with recognised secrets replaced by :data:`PLACEHOLDER`."""
    if not text:
        return text
    return secretscrub.scrub_text(text, PLACEHOLDER)


def redact_obj(obj: Any) -> Any:
    """Recursively redact every string inside a JSON-ish structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
