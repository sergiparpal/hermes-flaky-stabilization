"""The single owner of credential / secret *shape* detection (stdlib only).

Three stages needed to scrub secrets from text on a model-facing or outbound
path, and each had grown its own copy of the token regexes:

* ``triage/redact.py``            — scrubs CI-log excerpts before the LLM sees them;
* ``healer/flaky_healer/redact.py`` — masks the healer's audit trail;
* ``pii/redaction.py``            — redacts outbound Jira incident text.

The copies had drifted and, worse, covered *different* secret formats — only
triage caught Slack/Google/``sk-`` keys, so one of those leaking into a healer
audit row or a Jira field went out in the clear. This module holds the union of
those shapes once; a new secret format is added here and every consumer gets it.

What stays module-specific (and belongs there): the *replacement token* each
consumer emits (``<REDACTED>`` vs ``***`` vs ``[redacted-secret]``) and the
*recursive-walk policy* (whether a sensitive dict key masks its whole value).
Those are parameters/thin wrappers over the primitives below, not copies of the
pattern set.

Pure standard library; imports nothing from the plugin, so it is the bottom of
the dependency stack and any stage may depend on it.
"""

from __future__ import annotations

import re

DEFAULT_PLACEHOLDER = "<REDACTED>"

# --------------------------------------------------------------------------
# Standalone credential/token shapes — the whole match is the secret.
# --------------------------------------------------------------------------
# Ordered most-specific-first, though the prefixes are disjoint enough that at
# any position at most one alternative matches. Compiled together (one scan,
# DOTALL for the PEM body) into TOKEN_VALUE_RE below.
_TOKEN_SOURCES: tuple[str, ...] = (
    # GitHub fine-grained PAT (before the classic ``gh?_`` form so it is never
    # partially eaten). ``github`` does not match ``gh[poursa]_`` anyway.
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    # GitHub classic PAT / OAuth / app / refresh / actions token.
    r"\bgh[poursa]_[A-Za-z0-9]{20,}\b",
    # AWS access key ids (all documented prefixes, not just AKIA).
    r"\b(?:AKIA|ASIA|AGPA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{12,}\b",
    # Slack tokens.
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
    # Google API key.
    r"\bAIza[0-9A-Za-z_\-]{35,}\b",
    # OpenAI/Anthropic-style secret keys, incl. hyphenated forms (sk-proj-…).
    r"\bsk-[A-Za-z0-9_-]{20,}\b",
    # JWT (header.payload.signature).
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
    # Atlassian API tokens ("ATATT…" current, "ATCTT…" legacy).
    r"\bAT[A-Z]TT[A-Za-z0-9_\-=.]{8,}",
    # Generic Bearer token (value shape, caught even under an innocent key).
    r"[Bb]earer\s+[A-Za-z0-9._\-]{12,}",
    # PEM private-key blocks. The END marker is OPTIONAL: prefilter windowing
    # and char/line caps routinely cut a block before its END line, and an
    # unpaired BEGIN must still redact the key body to end-of-text rather than
    # let raw key material through. The lazy ``.*?`` stops at the earliest END.
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
    r".*?(?:-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----|\Z)",
)

# One compiled alternation. re.DOTALL so the PEM body may span newlines; the
# other alternatives use character classes (no bare ``.``), so DOTALL is a no-op
# for them. Exposed as a module attribute because the healer's audit store and
# handler assert they share the SAME object.
TOKEN_VALUE_RE = re.compile("|".join(f"(?:{p})" for p in _TOKEN_SOURCES), re.DOTALL)

# --------------------------------------------------------------------------
# Credentials embedded in a URL / connection string: scheme://user:pass@host.
# --------------------------------------------------------------------------
# Only the password segment (between ':' and '@') is replaced; the scheme, user,
# and host stay readable. The password class excludes '/' and '@' so the match
# cannot run past the authority, and is length-bounded.
URL_CRED_RE = re.compile(
    r"(?P<pre>\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]*:)(?P<pw>[^\s/@]{1,256})(?P<at>@)"
)

# --------------------------------------------------------------------------
# ``key = value`` / ``key: value`` assignments where the key names a secret.
# --------------------------------------------------------------------------
# Only the value is replaced; the key (and surrounding quotes, if any) are kept
# so the line stays readable. An explicit ':'/'=' separator is required, so
# prose such as "token not found" (no separator) is left intact. Both value
# branches are length-bounded so a hostile line cannot backtrack.
KV_RE = re.compile(
    r"(?P<key>\b[\w.-]*?"
    r"(?:authorization|bearer|client[_-]?secret|private[_-]?key|access[_-]?key"
    r"|api[_-]?key|apikey|secret|password|passwd|pwd|token)\b)"
    r"(?P<sep>[\"']?\s*[:=]\s*)"
    r"(?:"
    r"(?P<q>[\"'])(?P<qval>(?:bearer\s+)?[^\"'\r\n]{1,4096})(?P=q)"
    r"|"
    r"(?P<val>(?:bearer\s+)?[^\s\"',;]{1,4096})"
    r")",
    re.IGNORECASE,
)

# Dict-key names whose *value* is a credential — for the structured recursive
# walk (a token pasted under such a key may not be token-shaped).
SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|access[_-]?key|bearer)",
    re.I,
)


def _url_cred_sub(placeholder: str):
    def sub(m: re.Match) -> str:
        return f"{m.group('pre')}{placeholder}{m.group('at')}"
    return sub


def _kv_sub(placeholder: str):
    def sub(m: re.Match) -> str:
        if m.group("qval") is not None:
            q = m.group("q")
            return f"{m.group('key')}{m.group('sep')}{q}{placeholder}{q}"
        return f"{m.group('key')}{m.group('sep')}{placeholder}"
    return sub


def mask_token_shapes(text: str, placeholder: str = DEFAULT_PLACEHOLDER) -> str:
    """Mask every standalone token-shaped substring in *text*.

    The value-shape half of secret scrubbing: it catches a token under an
    innocent key or embedded in free-form text. Does NOT touch ``key=value``
    assignments or URL credentials — use :func:`scrub_text` for a full pass.
    """
    return TOKEN_VALUE_RE.sub(placeholder, text)


def scrub_text(text: str, placeholder: str = DEFAULT_PLACEHOLDER) -> str:
    """Return *text* with recognised secrets replaced by *placeholder*.

    Applies, in order, the standalone token shapes, URL-embedded credentials,
    and ``key: value`` secret assignments. Falsy input is returned unchanged so
    callers keep their ``None``/``""`` semantics.
    """
    if not text:
        return text
    out = TOKEN_VALUE_RE.sub(placeholder, text)
    out = URL_CRED_RE.sub(_url_cred_sub(placeholder), out)
    out = KV_RE.sub(_kv_sub(placeholder), out)
    return out
