"""PII redaction for Jira incident data.

Every path where indexed incident content can reach the model — the
``system_prompt_block()``, ``prefetch()`` output, and every tool result —
runs through this module first.  Redaction is mandatory (see the plugin
README / plan §3.7): the model must never see raw PII.

Design notes
------------
* Pure functions, no side effects, standard library only.  This module is
  intentionally free of any Hermes imports so it can be unit-tested in
  isolation and reused anywhere.
* The redactors fall on the conservative side: when in doubt they redact.
  Over-redaction costs a little recall; under-redaction leaks PII, which is
  the failure mode we refuse.  Each redactor replaces with a stable
  ``[redacted-*]`` token so output stays readable and assertions are simple.
* Free-form personal-name detection without an NLP model is unreliable, so
  names are handled two ways: (1) structured ``reporter``/``assignee`` field
  *values* are replaced wholesale by :func:`redact_incident`, and (2) names
  that appear behind an explicit label in free text ("Reporter: Jane Doe")
  are caught by the labelled-name pattern below.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..common import secretscrub
from . import _patterns

# Hard cap on the amount of text a single redaction pass will scan. Incident
# fields are normally small; an abnormally large field (pathological or
# hostile) could otherwise turn the regex sweep into a CPU sink on the
# synchronous tool path. Truncating to a generous bound keeps redaction linear
# and bounded without affecting any realistic incident content.
MAX_REDACT_CHARS = 200_000
TRUNCATION_MARKER = " […truncated]"
# When truncating oversized input, back off to a whitespace boundary within
# this many trailing chars so a PII token straddling the cut is dropped whole
# rather than severed into an unredactable fragment (e.g. half an email). Any
# realistic PII token (email, card, key, URL) is far shorter than this.
TRUNCATE_BACKOFF = 512

# Replacement tokens (kept distinct so callers/tests can tell what fired).
EMAIL_TOKEN = "[redacted-email]"
PHONE_TOKEN = "[redacted-phone]"
SECRET_TOKEN = "[redacted-secret]"
IP_TOKEN = "[redacted-ip]"
SSN_TOKEN = "[redacted-ssn]"
CARD_TOKEN = "[redacted-card]"
NAME_TOKEN = "[redacted-name]"
URL_CRED_TOKEN = "[redacted-url-credentials]"

# Incident fields whose *value* is treated as a personal name and redacted
# wholesale by redact_incident().
NAME_FIELDS: tuple[str, ...] = ("reporter", "assignee", "creator", "contact", "caller")

# Common English words (and frequent name/word homographs) that must NOT be
# scrubbed from free text just because they happen to be a person's name part.
# Without this guard a reporter named "Will"/"Mark"/"Rose" would blank out the
# ordinary words "will"/"mark"/"rose" everywhere they appear (over-redaction).
# Full multi-word names and distinctive parts are still scrubbed; only these
# ambiguous single tokens are spared. Recall trade-off is intentional.
_NAME_STOPWORDS = frozenset({
    # name/word homographs
    "will", "mark", "rose", "hope", "grant", "dawn", "drew", "bill", "june",
    "april", "august", "grace", "faith", "guy", "page", "lane", "reed",
    "brook", "dale", "dean", "earl", "rich", "miles", "summer", "ivy",
    "holly", "daisy", "olive", "pearl", "ruby", "jade", "robin", "sandy",
    "victor", "noel", "mercy", "sunny", "long", "short", "case", "white",
    "brown", "green", "black", "gray", "grey", "young", "king", "lord",
    "love", "joy", "art", "ray",
    # very common ordinary words that could collide
    "this", "that", "with", "from", "they", "them", "then", "than", "what",
    "when", "where", "which", "would", "could", "should", "about", "there",
    "their", "here", "your", "ours", "were", "have", "been", "does", "done",
    "into", "over", "under", "more", "most", "some", "such", "only", "very",
    "much", "many", "each", "both", "must", "just", "like", "time", "team",
    "work", "code", "data", "user", "host", "down", "open", "fail", "root",
    "call", "line", "node", "pool", "lock", "sync", "test", "real", "full",
    "free", "busy", "dead", "live", "null", "true", "false", "none",
})


def _compile(pattern: str, flags: int = 0) -> re.Pattern[str]:
    # Named `_compile` (not `_re`, which read as a typo next to `import re`).
    return re.compile(pattern, flags)


# Order matters: secrets and emails before phones/numbers so a token that
# contains digits is not partially eaten by the phone matcher first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --- Credentials embedded in URLs (user:pass@host) -------------------
    # IGNORECASE so an uppercase scheme (HTTPS://user:pass@…) is caught too;
    # the captured scheme keeps its original case via the \1 backreference.
    (_compile(r"\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s:@]+@", re.IGNORECASE),
     r"\1" + URL_CRED_TOKEN + "@"),

    # --- Secrets / tokens / keys ----------------------------------------
    # Standalone credential shapes owned by the shared secret scrubber (GitHub,
    # Slack, Google, sk-, JWT, AWS, Atlassian, Bearer, PEM). Sourced here so the
    # outbound Jira path catches the same formats as the triage/healer scrubbers
    # — the incidents redactor previously missed GitHub/Slack/Google/sk-/JWT, so
    # those leaked into tickets in the clear.
    (secretscrub.TOKEN_VALUE_RE, SECRET_TOKEN),
    # Atlassian API tokens (current "ATATT…" and legacy "ATCTT…" prefixes).
    (_compile(r"\bAT[A-Z]TT[A-Za-z0-9_\-=.]{8,}"), SECRET_TOKEN),
    # Atlassian OAuth / connect JWT-ish and generic Bearer tokens.
    (_compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}", re.IGNORECASE), SECRET_TOKEN),
    # AWS access key ids.
    (_compile(r"\bAKIA[0-9A-Z]{16}\b"), SECRET_TOKEN),
    # Private key blocks.
    (_compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), SECRET_TOKEN),
    # Labelled secrets: api_key=..., token: ..., password = ...
    # The value runs to the end of the line so multi-word / space-containing
    # secrets ("password: hunter two three") are redacted whole rather than
    # leaking everything after the first space. Over-redaction here is the safe
    # direction; the label still requires an explicit ':'/'=' separator so
    # ordinary prose ("the password was rotated") is untouched.
    (_compile(r"(?i)\b(api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|token|secret|password|passwd|pwd|client[_-]?secret)\b\s*[:=]\s*[\"']?[^\r\n]+",
         ), lambda m: _label_replace(m, SECRET_TOKEN)),

    # --- Email addresses -------------------------------------------------
    # Composed from the shared EMAIL_BODY (see _patterns.py for the length-bound /
    # Unicode-local-part rationale). This path is recall-biased, so the TLD bound
    # is {2,} and the pattern is \b-anchored — DELIBERATELY not the {2,24}, un-
    # anchored form the detector uses (see _patterns.py); do not merge them.
    (_compile(r"\b" + _patterns.EMAIL_BODY + r"[A-Za-z]{2,}\b"), EMAIL_TOKEN),

    # --- Government / financial identifiers -----------------------------
    # US SSN. Space- and dash-separated (env dumps and prose use either); the
    # separator class is [ \t\-] so the match cannot span a newline.
    (_compile(r"\b\d{3}[ \t\-]\d{2}[ \t\-]\d{4}\b"), SSN_TOKEN),
    # Credit-card-like 13–16 digit groups (with optional spaces/dashes).
    # Anchored to start and end on a digit so a trailing separator (and the
    # following word) is not swallowed into the match.
    (_compile(r"\b\d(?:[ \-]?\d){12,15}\b"), CARD_TOKEN),

    # --- IPv4 addresses --------------------------------------------------
    (_compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), IP_TOKEN),

    # --- IPv6 addresses --------------------------------------------------
    # Full and every `::`-compressed form. All quantifiers are bounded ({1,7}),
    # so there is no catastrophic backtracking. Lookarounds (not \b, which does
    # not assert cleanly next to a leading/trailing ':') keep it from splicing a
    # match out of an adjacent hex run; a `HH:MM:SS` timestamp (3 groups, single
    # colons) and a MAC (6 groups, no `::`) do not match.
    (_compile(r"(?<![\w:.])(?:"
         r"(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}"
         r"|(?:[A-Fa-f0-9]{1,4}:){1,7}:"
         r"|(?:[A-Fa-f0-9]{1,4}:){1,6}:[A-Fa-f0-9]{1,4}"
         r"|(?:[A-Fa-f0-9]{1,4}:){1,5}(?::[A-Fa-f0-9]{1,4}){1,2}"
         r"|(?:[A-Fa-f0-9]{1,4}:){1,4}(?::[A-Fa-f0-9]{1,4}){1,3}"
         r"|(?:[A-Fa-f0-9]{1,4}:){1,3}(?::[A-Fa-f0-9]{1,4}){1,4}"
         r"|(?:[A-Fa-f0-9]{1,4}:){1,2}(?::[A-Fa-f0-9]{1,4}){1,5}"
         r"|[A-Fa-f0-9]{1,4}:(?::[A-Fa-f0-9]{1,4}){1,6}"
         r"|:(?:(?::[A-Fa-f0-9]{1,4}){1,7}|:)"
         r")(?![\w:.])"), IP_TOKEN),

    # --- Phone numbers ---------------------------------------------------
    # International (+CC ...) and separated domestic forms with 7–15 digits.
    # The separator class is space/tab (not \s) so the match cannot span a
    # newline and swallow the following line of unrelated text.
    (_compile(r"(?<![\w.])\+\d[\d \t().\-]{6,}\d"), PHONE_TOKEN),
    (_compile(r"(?<![\w.])\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"), PHONE_TOKEN),

    # --- Labelled personal names ----------------------------------------
    (_compile(r"(?i)\b(reporter|reported by|assignee|assigned to|owner|contact|requested by|requester|caller|created by|creator)\b\s*[:\-]?\s+([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){0,3})"),
        lambda m: f"{m.group(1)}: {NAME_TOKEN}"),
]


# --- Prompt-injection neutralisation (untrusted incident text) -------------
# Incident content (summary/body/root-cause/notes) is attacker-influenceable —
# anyone able to file or edit a ticket controls text that this plugin injects
# into the model every turn. Redaction strips PII; it does not stop a malicious
# ticket from *instructing* the agent. These two transforms defang the most
# direct vectors before incident text reaches the model.

# Control characters (C0/C1) other than tab/newline can hide content from a
# human reviewer or smuggle formatting; strip them from model-facing text.
_CONTROL_CHARS_RE = _compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# A line that begins with a chat/role marker ("System:", "Assistant:") is a
# classic injection trying to forge a conversation turn. Neutralise only the
# delimiter (turn the ':' into ' -') so the text is preserved but can no longer
# read as a role boundary.
_ROLE_MARKER_RE = _compile(r"(?im)^([ \t]*)(system|assistant|user|developer|human|ai|tool)([ \t]*):")


def _coerce_str(text: Any) -> str:
    """Coerce untrusted input to a string: ``None`` -> ``""``, else ``str(text)``.

    The single source of the "None means empty, everything else is stringified"
    rule shared by the model-facing helpers, so the four entry points can't drift
    on how a non-string reaches the regex sweep. The empty-input early return
    stays at each call site — two of the callers return ``""`` on empty, the
    other two do not, so that decision is theirs to make.
    """
    if text is None:
        return ""
    return text if isinstance(text, str) else str(text)


def neutralize_untrusted(text: Any) -> str:
    """Defang prompt-injection constructs in untrusted incident text.

    Strips control characters and neutralises line-leading chat/role markers so
    attacker-supplied incident content cannot be read by the model as a fake
    conversation turn. Orthogonal to PII redaction (applied separately) and, like
    it, never raises — this is on the model-facing path.
    """
    text = _coerce_str(text)
    if not text:
        return ""
    try:
        text = _CONTROL_CHARS_RE.sub("", text)
        text = _ROLE_MARKER_RE.sub(r"\1\2\3 -", text)
    except Exception:
        return text
    return text


def _label_replace(m: re.Match[str], token: str) -> str:
    """Keep the ``label:`` prefix (and its original spacing), redact the value."""
    text = m.group(0)
    # Prefix = everything up to and including the separator and any following
    # whitespace; the value (incl. an optional opening quote) is replaced.
    prefix = re.match(r".*?[:=]\s*", text, flags=re.DOTALL)
    if not prefix:
        return token
    return prefix.group(0) + token


def redact_text(text: Any) -> str:
    """Return *text* with all detectable PII replaced by ``[redacted-*]`` tokens.

    Non-string input is coerced to ``str``.  Always returns a string and
    never raises — redaction must not be a source of failures on the
    model-facing path.
    """
    text = _coerce_str(text)
    if not text:
        return ""
    # Bound the work: keep the redaction sweep linear even on a hostile,
    # oversized field (see MAX_REDACT_CHARS).
    if len(text) > MAX_REDACT_CHARS:
        cut = text[:MAX_REDACT_CHARS]
        # Back off to the last whitespace in the final TRUNCATE_BACKOFF chars so
        # a PII token cut by the hard boundary loses its surviving prefix (a
        # raw slice could leave e.g. "joe@e" before the marker). If the tail is
        # one long whitespace-free run we keep the hard cut — a bare fragment
        # with no surrounding structure isn't a usable identifier, and this
        # preserves the bounded-length guarantee tested by callers.
        window = MAX_REDACT_CHARS - TRUNCATE_BACKOFF
        ws = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
        if ws >= window:
            cut = cut[:ws]
        text = cut + TRUNCATION_MARKER
    try:
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
    except Exception:
        # Defensive: a pathological input must not break the turn. Fall back
        # to a coarse scrub of the obvious identifiers.
        text = re.sub(r"\S+@\S+", EMAIL_TOKEN, text)
    return text


def _collect_known_names(incident: dict[str, Any]) -> list[str]:
    """Gather person-name strings from the structured name fields.

    Returns the full (multi-word) names plus their distinctive individual parts
    (>= 4 chars, not a common-word homograph), longest-first so multi-word
    names are scrubbed before their parts. Single-token names that are common
    words (e.g. a reporter literally named "Will") are deliberately *not* added
    to the free-text scrub list, so ordinary prose is not mangled; the
    structured field itself is still redacted wholesale by redact_incident().
    """
    names: set[str] = set()
    for field in NAME_FIELDS:
        value = incident.get(field)
        if not (isinstance(value, str) and value.strip()):
            continue
        full = value.strip()
        parts = re.split(r"\s+", full)
        if len(parts) > 1:
            names.add(full)
        for part in parts:
            if len(part) >= 4 and part[:1].isalpha() and part.lower() not in _NAME_STOPWORDS:
                names.add(part)
    return sorted(names, key=len, reverse=True)


def _scrub_names(text: str, names: list[str]) -> str:
    """Replace each known name (whole word, case-insensitive) with the token.

    Builds a single alternation regex instead of one sub() pass per name. The
    *names* list is ordered longest-first by the caller, and Python's regex
    alternation is leftmost-first, so a full multi-word name is preferred over
    its individual parts at any given position.
    """
    if not names:
        return text
    alternation = "|".join(re.escape(name) for name in names)
    return re.sub(r"\b(?:" + alternation + r")\b", NAME_TOKEN, text, flags=re.IGNORECASE)


def redact_incident(incident: dict[str, Any],
                    fields: Iterable[str] | None = None) -> dict[str, Any]:
    """Return a copy of *incident* safe to expose to the model.

    * Name-bearing fields (reporter, assignee, …) are replaced wholesale
      with ``[redacted-name]`` when present and non-empty.
    * Free-text fields (summary, root_cause, body, status, …) are run through
      :func:`redact_text`, then have any *known* person names (taken from this
      incident's structured name fields) scrubbed — so a reporter named in the
      summary is caught even without a label, without over-redacting ordinary
      capitalised phrases.
    * Unknown keys are passed through :func:`redact_text` if they are
      strings, otherwise copied as-is.

    When *fields* is given (an iterable of key names), only those keys are
    redacted and returned — callers that surface just a subset (e.g. search
    shows only key/summary/status) avoid paying the regex sweep over large
    fields like ``body`` that they never emit. Known person names are still
    derived from the *full* incident, so name scrubbing stays correct even
    when the name fields themselves aren't in the output subset.
    """
    if not isinstance(incident, dict):
        return {}
    known_names = _collect_known_names(incident)
    if fields is None:
        items = incident.items()
    else:
        items = [(k, incident.get(k)) for k in fields]
    out: dict[str, Any] = {}
    for key, value in items:
        if key in NAME_FIELDS:
            out[key] = NAME_TOKEN if (value not in (None, "", [])) else value
        elif isinstance(value, str):
            scrubbed = redact_text(value)
            if known_names:
                scrubbed = _scrub_names(scrubbed, known_names)
            # Defang injection constructs after PII is removed: incident free
            # text is untrusted and reaches the model verbatim.
            scrubbed = neutralize_untrusted(scrubbed)
            out[key] = scrubbed
        else:
            out[key] = value
    return out


def redact_note(note: Any, incident: dict[str, Any] | None = None) -> str:
    """Redact a free-text note for the model-facing path.

    Runs the standard :func:`redact_text` pass and, when an *incident* is
    supplied, additionally scrubs that incident's known person names — so a
    bare name in a user-supplied link note ("called Jane Doe") is caught even
    without a label, consistent with the other tool/prefetch paths.
    """
    safe = redact_text(note)
    if isinstance(incident, dict):
        names = _collect_known_names(incident)
        if names:
            safe = _scrub_names(safe, names)
    # The note is attacker-influenceable free text on a model-facing path.
    return neutralize_untrusted(safe)


def contains_pii(text: Any) -> bool:
    """True if :func:`redact_text` would change *text* (i.e. PII detected)."""
    if text is None:
        return False
    original = _coerce_str(text)
    return redact_text(original) != original


def neutralization_needed(text: Any) -> bool:
    """True if :func:`neutralize_untrusted` would change *text*.

    Complements :func:`contains_pii` so the egress canary can also flag
    injection residue — control characters or a forged line-leading role marker
    that reached a model-facing path unneutralised.
    """
    if text is None:
        return False
    original = _coerce_str(text)
    return neutralize_untrusted(original) != original
