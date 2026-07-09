"""Healer stage (ported from hermes-flaky-healer).

Exposes ``register(ctx)``. This module is deliberately thin: schemas live in
schemas.py, handlers in handlers.py, logic in flaky_healer/. It must import
cleanly without Hermes installed. Unified-plugin adaptation: the skill file
now lives at the unified repo root (``skills/flaky-healer/SKILL.md``), so the
qualified skill name becomes ``hermes-flaky-stabilization:flaky-healer``
(documented breaking change, plan §10).
"""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path

try:
    from . import handlers, schemas
except ImportError:  # loaded as a flat module: fall back to path imports
    import sys

    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)
    import handlers
    import schemas

log = logging.getLogger("hermes-flaky-healer")

TOOLSET = "flaky_healer"


def _github_token_present() -> bool:
    """check_fn for fetch_ci_logs: hide the tool when GITHUB_TOKEN is absent.

    Deliberately NOT a manifest requires_env entry — that would disable the
    whole plugin, while trace analysis and recipes work fine without GitHub.
    """
    return bool(os.environ.get("GITHUB_TOKEN"))


def _log_host_version() -> None:
    """No hard minimum Hermes version; record it when available (plan §3.2)."""
    try:
        from hermes_cli import __version__ as hermes_version  # type: ignore

        log.debug("registering against Hermes %s", hermes_version)
    except Exception:  # noqa: BLE001 — Hermes absent (tests) or layout changed
        log.debug("hermes_cli version not detectable; continuing")


def register(ctx) -> None:
    _log_host_version()
    bind = lambda fn: functools.partial(fn, ctx=ctx)  # noqa: E731

    ctx.register_tool(
        "fetch_ci_logs",
        toolset=TOOLSET,
        schema=schemas.FETCH_CI_LOGS,
        handler=bind(handlers.fetch_ci_logs),
        check_fn=_github_token_present,
    )
    ctx.register_tool(
        "analyze_playwright_trace",
        toolset=TOOLSET,
        schema=schemas.ANALYZE_PLAYWRIGHT_TRACE,
        handler=bind(handlers.analyze_playwright_trace),
    )
    ctx.register_tool(
        "heal_flaky_test",
        toolset=TOOLSET,
        schema=schemas.HEAL_FLAKY_TEST,
        handler=bind(handlers.heal_flaky_test),
    )
    ctx.register_tool(
        "list_healing_recipes",
        toolset=TOOLSET,
        schema=schemas.LIST_HEALING_RECIPES,
        handler=bind(handlers.list_healing_recipes),
    )

    # Bind ctx so the audit store resolves to the same profile dir the tools use
    # (an unbound hook cannot see ctx.hermes_home and would split the audit trail).
    ctx.register_hook("pre_approval_request", bind(handlers.on_pre_approval_request))
    ctx.register_hook("post_approval_response", bind(handlers.on_post_approval_response))

    skill_path = Path(__file__).resolve().parents[2] / "skills" / "flaky-healer" / "SKILL.md"
    try:
        # A real Path, not str: the host's register_skill calls path.exists()
        # on the argument (an integration bug the legacy plugin's str silently
        # tripped — caught by the Phase-8 real-loader tests).
        ctx.register_skill("flaky-healer", skill_path)
    except Exception as exc:  # noqa: BLE001 — older hosts may lack register_skill
        log.debug("register_skill unavailable or failed: %s", exc)

    try:
        ctx.register_command(
            "heal",
            bind(handlers.heal_command),
            description="Heal a flaky Playwright test: /heal <repo_dir> <test_id> [suggest|pr]",
        )
    except Exception as exc:  # noqa: BLE001 — older hosts may lack register_command
        log.debug("register_command unavailable or failed: %s", exc)
