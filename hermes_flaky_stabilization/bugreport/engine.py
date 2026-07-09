"""The model call: prompt assembly, the structured request, and retry policy.

``build_report`` is the high-level operation — given the validated input, return
a normalized :class:`~domain.ImprovedBugReport` or raise. It makes one structured
call and, if that yields nothing usable, retries once with a stricter directive
and a larger token budget. This module is the only one that knows the host LLM
contract beyond the ``host`` Protocols: the generation settings, and the
schema/purpose passed to ``complete_structured``, live here.
"""

from __future__ import annotations

import logging

from . import prompts, schema
from .domain import ImprovedBugReport
from .host import HostContext, LlmClient, LlmUnavailable

logger = logging.getLogger(__name__)

# Generation settings for the structured call. The retry uses a larger token
# budget so a first attempt that failed by truncation (e.g. a long verbatim
# stack trace) has room to complete the second time.
_MAX_TOKENS = 2048
_RETRY_MAX_TOKENS = 4096
_TEMPERATURE = 0.0
_SCHEMA_NAME = "bug_report.improved"
_PURPOSE = "hermes-bug-report-improver.improve_bug_report"
_RETRY_SUFFIX = (
    "Your previous response was not valid JSON matching the schema. Reply with "
    "valid JSON only — no Markdown, no code fences, no commentary — using exactly "
    "the required fields."
)


def _input_blocks(raw_text: str, context: str) -> list[dict[str, str]]:
    extra = context.strip()
    text = f"{raw_text}\n\n[Additional context: {extra}]" if extra else raw_text
    return [{"type": "text", "text": text}]


def _attempt(
    llm: LlmClient, instructions: str, input_blocks: list[dict[str, str]], max_tokens: int
) -> ImprovedBugReport | None:
    """One structured call. Returns a coerced report, or None if unusable.

    A ``ValueError`` from ``complete_structured`` — e.g. a host that enforces
    the schema with the optional ``jsonschema`` package rejected the model's
    output — is treated as an unusable result so the caller retries, mirroring
    the path taken when the host returns ``parsed`` for us to validate.
    Non-``ValueError`` failures (trust gate, auth, network) still propagate.
    """
    try:
        result = llm.complete_structured(
            instructions=instructions,
            input=input_blocks,
            json_schema=schema.BUG_REPORT_OUTPUT_SCHEMA,
            schema_name=_SCHEMA_NAME,
            purpose=_PURPOSE,
            temperature=_TEMPERATURE,
            max_tokens=max_tokens,
        )
    except ValueError as exc:  # host-side schema/parse rejection -> retryable
        logger.debug("complete_structured rejected the call: %s", exc)
        return None
    parsed = getattr(result, "parsed", None)
    if parsed is None:
        logger.debug("complete_structured returned no parsed JSON")
        return None
    try:
        return ImprovedBugReport.from_parsed(parsed)
    except ValueError as exc:
        logger.debug("parsed output failed validation: %s", exc)
        return None


def build_report(ctx: HostContext, raw_text: str, context: str) -> ImprovedBugReport:
    """Delegate the rewrite to the agent's model. Raise on unrecoverable failure."""
    llm = getattr(ctx, "llm", None)
    if llm is None:
        raise LlmUnavailable("ctx.llm is not available")

    instructions = prompts.build_instructions()
    blocks = _input_blocks(raw_text, context)

    report = _attempt(llm, instructions, blocks, _MAX_TOKENS)
    if report is None:  # retry once with a stricter directive and more room
        report = _attempt(llm, f"{instructions}\n\n{_RETRY_SUFFIX}", blocks, _RETRY_MAX_TOKENS)
    if report is None:
        raise ValueError("model did not return a valid structured report after one retry")
    return report
