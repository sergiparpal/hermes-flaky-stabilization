"""Plugin hooks (plan §6.2): the unified plugin's lifecycle wiring.

Registered here rather than inside the stages so the full hook surface is
visible in one place:

* ``pre_llm_call``       — incident context injection (plan D1): a config-gated,
                           local-FTS-only, timeout-bounded, redacted lookup of
                           the user message; returns ``{"context": ...}`` or
                           ``None``. Never network I/O on the turn path.
* ``on_session_start``   — non-blocking background Jira sync trigger (silent
                           no-op without credentials).
* ``pre_approval_request`` / ``post_approval_response`` — the healer's audit
                           observers (registered by the healer stage itself).

Phase 6 adds ``pre_tool_call`` (approval escalation for ``heal_flaky_test``
mode=pr and ``jira_create_incident`` — plan D6.3).

Hook callbacks must accept ``**kwargs`` and never raise (the host swallows
exceptions to WARNING, but a broken hook still wastes a turn's error budget).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx, *, incidents_service) -> None:
    """Wire the lifecycle hooks around the incidents service."""

    def on_pre_llm_call(user_message: str = "", **kwargs):
        try:
            return incidents_service.llm_context(user_message)
        except Exception:  # defensive: the service already guards internally
            logger.debug("incident context injection failed", exc_info=True)
            return None

    def on_session_start(session_id: str = "", **kwargs):
        try:
            incidents_service.on_session_start(session_id)
        except Exception:
            logger.debug("session-start sync trigger failed", exc_info=True)
        return None

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("on_session_start", on_session_start)
