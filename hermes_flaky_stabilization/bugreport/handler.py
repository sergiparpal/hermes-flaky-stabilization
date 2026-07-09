"""Entry points for the ``improve_bug_report`` tool and ``/improve-bug`` command.

Tool handlers in Hermes receive a single ``args`` dict and must return a string,
never raising. They do not receive ``ctx`` directly, so the handler is produced
by ``make_handler(ctx)`` and closes over ``ctx`` to reach ``ctx.llm`` (the same
pattern the official ``plugin-llm-example`` uses).

This module is orchestration only: it validates the input (``validation``),
builds the report (``engine``), and formats the result (``rendering``). The two
entry points share ``_improve``, which returns an ``_Outcome`` — a report or an
error — so each can format at its own edge rather than re-parsing the other's
serialized output.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import rendering, schema, validation
from .domain import ImprovedBugReport
from .engine import build_report
from .host import HostContext, LlmUnavailable

logger = logging.getLogger(__name__)


# --- Error envelope ------------------------------------------------------------
def _error(message: str) -> str:
    """Structured error string. Handlers must return a string, never raise."""
    return json.dumps({"error": message}, ensure_ascii=True)


# --- Orchestration -------------------------------------------------------------
@dataclass
class _Outcome:
    """Result of an improve request: either a ``report`` to format or an ``error``.

    Both entry points (the ``improve_bug_report`` tool and the ``/improve-bug``
    command) build one of these and format it at their own edge, so neither has
    to recover structure by re-parsing the other's serialized output.
    """

    report: ImprovedBugReport | None = None
    error: str | None = None
    fmt: str = schema.DEFAULT_FORMAT


def _improve(ctx: HostContext, args: Any) -> _Outcome:
    """Validate the input and build the structured report. Never raises."""
    try:
        raw_text, context, fmt = validation.validate_input(args)
    except ValueError as exc:
        return _Outcome(error=str(exc))

    try:
        report = build_report(ctx, raw_text, context)
    except LlmUnavailable as exc:
        return _Outcome(error=f"LLM is unavailable: {exc}")
    except ValueError as exc:
        return _Outcome(error=f"could not build a structured report: {exc}")
    except Exception:  # noqa: BLE001 - the tool path must never raise
        # Log the detail (with traceback) but report a generic message: the
        # exception text may carry host/library internals we should not leak.
        logger.warning("improve_bug_report failed", exc_info=True)
        return _Outcome(error="internal error while improving the report")

    return _Outcome(report=report, fmt=fmt)


# --- Public factories ----------------------------------------------------------
def make_handler(ctx: HostContext) -> Callable[..., str]:
    """Build the tool handler, closing over ``ctx`` for ``ctx.llm`` access."""

    def improve_bug_report(args: Any = None, **kwargs: Any) -> str:
        # Hermes may invoke a handler positionally with the args dict or via
        # kwargs; accept either, then run the shared validate+build pipeline.
        if not isinstance(args, dict):
            args = kwargs or {}
        outcome = _improve(ctx, args)
        if outcome.error is not None or outcome.report is None:
            return _error(outcome.error or "internal error while improving the report")
        return rendering.format_output(outcome.report, outcome.fmt)

    improve_bug_report.__name__ = schema.TOOL_NAME
    return improve_bug_report


def make_command(ctx: HostContext) -> Callable[[str], str]:
    """Build the optional ``/improve-bug`` slash-command handler.

    Slash-command handlers receive the raw argument string and return a string
    shown directly to the user, so this renders Markdown on success and turns a
    failure into one readable line — sharing ``_improve`` with the tool rather
    than re-parsing the tool's serialized output.
    """

    def improve_bug(raw_args: str = "") -> str:
        text = (raw_args or "").strip()
        if not text:
            return "Usage: /improve-bug <paste the raw bug report text>"
        outcome = _improve(ctx, {"raw_text": text, "format": "markdown"})
        if outcome.error is not None or outcome.report is None:
            return f"Could not improve the report: {outcome.error or 'internal error'}"
        return rendering.render_markdown(outcome.report)

    return improve_bug
