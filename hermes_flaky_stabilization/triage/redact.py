"""Secret redaction for log excerpts before they leave the host.

CI logs routinely contain credentials (tokens, keys, connection strings). This
module scrubs the common formats from any text that is about to be (a) sent to
the LLM provider for classification, (b) forwarded to another tool, or (c)
echoed back in the tool's result — so a secret that leaked into a build log is
not propagated or reflected further.

Pure standard library; no Hermes dependency. Conservative by design: it targets
well-known secret *shapes* and explicit ``key = value`` secret assignments, so
it does not mangle ordinary failure text (a failure signal is never a token).
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "<REDACTED>"

# Standalone secret formats — the whole match is replaced.
_TOKEN_PATTERNS = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),            # GitHub PAT/OAuth/app/refresh
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),          # GitHub fine-grained PAT
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{12,}\b"),  # AWS key id
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),          # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35,}\b"),               # Google API key
    # OpenAI/Anthropic-style secret keys, incl. hyphenated forms (sk-proj-…, sk-ant-…).
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),  # JWT
    # PEM private-key blocks. The END marker is OPTIONAL: prefilter windowing
    # and char/line caps routinely cut a block before its END line, and an
    # unpaired BEGIN must still redact the key body (to end-of-text) rather
    # than let raw key material through. The paired form matches first (the
    # lazy `.*?` stops at the earliest END marker).
    re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
        r".*?(?:-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
]

# Credentials embedded in a URL / connection string: scheme://user:pass@host.
# Only the password segment (between the ':' and the '@') is replaced; the
# scheme, user, and host stay readable. The password class excludes '/' and '@'
# so the match cannot run past the authority, and is length-bounded.
_URL_CRED_PATTERN = re.compile(
    r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]*:)(?P<pw>[^\s:/@]{1,256})(?P<at>@)"
)


def _url_cred_sub(m: re.Match) -> str:
    return f"{m.group('pre')}{PLACEHOLDER}{m.group('at')}"


# ``key = value`` / ``key: value`` assignments where the key names a secret.
# Only the value is replaced; the key (and surrounding quotes, if any) are kept
# so the line stays readable for triage. An explicit ':' or '=' separator is
# required, so prose such as "token not found" (no separator) is left intact.
#
# The separator may absorb a closing quote on the key (``"password": …`` in
# JSON/dict output), and the value is matched two ways:
#   * quoted — capture everything up to the closing quote, so a multi-word
#     secret (``password = "correct horse battery"``) is redacted whole;
#   * unquoted — the whitespace/comma/semicolon-delimited token (>=4 chars).
# Both branches are length-bounded so a hostile line cannot backtrack.
_KV_PATTERN = re.compile(
    r"(?P<key>\b[\w.-]*?"
    r"(?:authorization|bearer|client[_-]?secret|private[_-]?key|access[_-]?key"
    r"|api[_-]?key|apikey|secret|password|passwd|pwd|token)\b)"
    r"(?P<sep>[\"']?\s*[:=]\s*)"
    r"(?:"
    r"(?P<q>[\"'])(?P<qval>(?:bearer\s+)?[^\"'\r\n]{1,4096})(?P=q)"
    r"|"
    r"(?P<val>(?:bearer\s+)?[^\s\"',;]{4,})"
    r")",
    re.IGNORECASE,
)


def _kv_sub(m: re.Match) -> str:
    if m.group("qval") is not None:
        q = m.group("q")
        return f"{m.group('key')}{m.group('sep')}{q}{PLACEHOLDER}{q}"
    return f"{m.group('key')}{m.group('sep')}{PLACEHOLDER}"


def redact(text: str) -> str:
    """Return *text* with recognised secrets replaced by :data:`PLACEHOLDER`."""
    if not text:
        return text
    out = text
    for pat in _TOKEN_PATTERNS:
        out = pat.sub(PLACEHOLDER, out)
    out = _URL_CRED_PATTERN.sub(_url_cred_sub, out)
    out = _KV_PATTERN.sub(_kv_sub, out)
    return out


def redact_obj(obj: Any) -> Any:
    """Recursively redact every string inside a JSON-ish structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
