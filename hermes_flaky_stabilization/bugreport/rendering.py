"""Rendering the canonical report to the user-facing output formats.

This module is the plugin's security surface, kept together on purpose. The
report text is attacker-influenced (a malicious bug report can steer the model,
and verbatim error text is echoed back), and the Markdown is shown to humans —
in a terminal for ``/improve-bug``, or rendered as HTML downstream.
``_sanitize_for_md`` is the chokepoint that makes a model-produced string safe
to embed. The ``json`` output is intentionally exempt (see ``format_output``).
"""

from __future__ import annotations

import json
import unicodedata

from .domain import ImprovedBugReport

# Block-level Markdown markers that open a new block when they start a line: ATX
# heading (#), bullet list (- * +), code fence (` and ~), and table (|).
# Blockquote (>) is defused separately by HTML-escaping it to &gt;; ordered-list
# markers ("1." / "1)") are handled in _escape_leading_block_marker below.
_LEADING_BLOCK_MARKERS = "#-*+`~|"


def _escape_leading_block_marker(text: str) -> str:
    """Backslash-escape a *leading* block-level Markdown marker.

    Each field is rendered at a block boundary, so a field whose text *begins*
    with a block marker would forge that block — a fake heading, list, code fence,
    or table — even after internal whitespace has been collapsed (collapsing only
    removes markers introduced by an embedded newline). Escaping just the leading
    marker neutralizes that without touching punctuation that legitimately appears
    mid-text, and it is visually lossless: ``\\#`` renders as ``#``. No-op on the
    empty string (guards against ``"" in _LEADING_BLOCK_MARKERS`` being true).
    """
    head = text[:1]
    if not head:
        return text
    if head in _LEADING_BLOCK_MARKERS:
        return "\\" + text
    if head.isdigit():
        # Ordered-list marker: digits, then "." or ")", then a space or end of
        # line. Escape the delimiter rather than prepending "\\" (a digit is not
        # punctuation, so "\\1" would render literally), and only for a real list
        # marker — so decimals and versions ("3.14", "v1.2.3") stay untouched.
        i = 1
        while i < len(text) and text[i].isdigit():
            i += 1
        if text[i : i + 1] in ".)" and text[i + 1 : i + 2] in ("", " "):
            return f"{text[:i]}\\{text[i:]}"
    return text


def _sanitize_for_md(text: str) -> str:
    """Make a model-produced string safe to embed in the rendered Markdown.

    The report text is ultimately attacker-influenced (a malicious bug report can
    steer the model, and verbatim error text is echoed back), and the Markdown is
    shown to humans — in a terminal for ``/improve-bug``, or rendered as HTML
    downstream. So before interpolating any field we:

    1. collapse all whitespace to single spaces, so an *embedded* newline cannot
       forge a new block (a fake ``## Severity`` heading or list item);
    2. drop Unicode control/format characters, removing the ESC byte (ANSI escape
       injection in a terminal) and bidi overrides (text-spoofing);
    3. HTML-escape ``& < >``, so the text cannot smuggle live HTML / ``<script>``
       through a renderer that passes raw HTML (this also defuses a leading ``>``
       blockquote);
    4. backslash-escape a *leading* block-level Markdown marker, so a field whose
       text begins with ``#``, ``-``/``*``/``+``, an ordered-list ``1.``/``1)``, a
       code fence, or a table ``|`` cannot forge that block at its block boundary
       (step 1 only neutralizes markers introduced by an embedded newline).

    Markdown link/image delimiters are escaped as well, preventing active links
    and remote images in renderers with permissive URL policies. The ``json``
    output remains structurally escaped by the JSON encoder.
    """
    text = " ".join(text.split())
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("[", "\\[").replace("]", "\\]")
    return _escape_leading_block_marker(text)


def _text_block(title: str, body: str) -> str:
    """A ``## heading`` followed by one sanitized text block (fallback if empty)."""
    return f"## {title}\n\n{_sanitize_for_md(body) or '_Not provided._'}"


def _list_block(title: str, items: list[str], empty: str, *, ordered: bool) -> str:
    """A ``## heading`` followed by a sanitized list (fallback text if empty)."""
    if not items:
        body = empty
    elif ordered:
        body = "\n".join(f"{i}. {_sanitize_for_md(s)}" for i, s in enumerate(items, 1))
    else:
        body = "\n".join(f"- {_sanitize_for_md(s)}" for s in items)
    return f"## {title}\n\n{body}"


def render_markdown(report: ImprovedBugReport) -> str:
    """Render the canonical report as Markdown; blocks are joined by blank lines."""
    blocks = [
        f"# {_sanitize_for_md(report.title)}",
        _sanitize_for_md(report.summary) or "_Not provided._",
        _list_block(
            "Reproduction Steps", report.reproduction_steps, "_None provided._", ordered=True
        ),
        _text_block("Expected Behavior", report.expected_behavior),
        _text_block("Actual Behavior", report.actual_behavior),
        _text_block(
            f"Severity: {_sanitize_for_md(report.severity)}", report.severity_rationale
        ),
        _list_block(
            "Missing Evidence",
            report.missing_evidence,
            "_None — the report appears complete._",
            ordered=False,
        ),
    ]
    return "\n\n".join(blocks)


def format_output(report: ImprovedBugReport, fmt: str) -> str:
    if fmt == "json":
        # ensure_ascii=True escapes U+2028/U+2029: valid in JSON but they break
        # JavaScript string literals if this JSON is later embedded in a page.
        return json.dumps(report.to_dict(), ensure_ascii=True, indent=2)
    return render_markdown(report)
