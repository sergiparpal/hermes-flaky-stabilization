"""hermes-bug-report-improver — a Hermes Agent plugin.

Registers a single tool, ``improve_bug_report``, in the ``qa`` toolset. Given a
poorly written or incomplete bug report, the tool returns a structured version
(title, reproduction steps, expected/actual behavior, suggested severity, and a
list of missing evidence) by delegating the rewrite to the agent's own model via
``ctx.llm.complete_structured`` — no provider keys live in this plugin.
"""

from __future__ import annotations

import logging

from . import schema
from .handler import make_command, make_handler
from .host import PluginHost

logger = logging.getLogger(__name__)


def register(ctx: PluginHost) -> None:
    """Entry point Hermes calls once at plugin load time."""
    tool_handler = make_handler(ctx)
    ctx.register_tool(
        name=schema.TOOL_NAME,
        toolset=schema.TOOLSET,
        schema=schema.TOOL_SCHEMA,
        handler=tool_handler,
    )
    logger.debug(
        "hermes-bug-report-improver: registered %s in toolset %r",
        schema.TOOL_NAME,
        schema.TOOLSET,
    )

    # Optional convenience: a /improve-bug slash command. Guarded so a signature
    # mismatch on some Hermes version never blocks the core tool from loading.
    try:
        ctx.register_command(
            name="improve-bug",
            handler=make_command(ctx),
            description="Rewrite a raw bug report into a structured one (Markdown).",
            args_hint="<raw bug report text>",
        )
    except Exception as exc:  # noqa: BLE001 - command is optional
        logger.warning("hermes-bug-report-improver: /improve-bug not registered: %s", exc)
