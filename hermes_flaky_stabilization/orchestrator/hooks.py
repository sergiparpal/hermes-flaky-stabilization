"""Plugin hooks (plan §6.2): the unified plugin's lifecycle wiring.

Registered here rather than inside the stages so the full hook surface is
visible in one place:

* ``pre_llm_call``       — incident context injection (plan D1): a config-gated,
                           local-FTS-only, timeout-bounded, redacted lookup of
                           the user message; returns ``{"context": ...}`` or
                           ``None``. Never network I/O on the turn path.
* ``on_session_start``   — non-blocking background Jira sync trigger (silent
                           no-op without credentials).
* ``pre_tool_call``      — approval escalation (plan D6.3, the *correct* Hermes
                           gate): ``{"action": "approve", ...}`` for
                           ``heal_flaky_test`` with ``mode="pr"`` and for every
                           ``jira_create_incident`` call. The host treats the
                           directive fail-closed (error/deny/timeout ⇒ blocked).
                           Pure dict inspection — no I/O on the hot path.
* ``pre_approval_request`` / ``post_approval_response`` — the healer's audit
                           observers (registered by the healer stage itself).

Hook callbacks must accept ``**kwargs`` and never raise (the host swallows
exceptions to WARNING, but a broken hook still wastes a turn's error budget).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

RULE_KEY_PR = "flaky_stab_pr"
RULE_KEY_TRACKER_WRITE = "flaky_stab_tracker_write"


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Escalate the two sensitive calls to the human approval gate (D6.3)."""
    if tool_name == "heal_flaky_test":
        mode = str((args or {}).get("mode") or "").strip().lower()
        if mode == "pr":
            return {
                "action": "approve",
                "message": (
                    "heal_flaky_test wants to open a pull request (branch, "
                    "commit, push, PR) for the healed test. Allow?"
                ),
                "rule_key": RULE_KEY_PR,
            }
    if tool_name == "jira_create_incident":
        return {
            "action": "approve",
            "message": (
                "jira_create_incident wants to create an issue in the external "
                "Jira tracker (PII-gated, redacted payload). Allow?"
            ),
            "rule_key": RULE_KEY_TRACKER_WRITE,
        }
    return None


def register(ctx, *, incidents_service) -> None:
    """Wire the lifecycle + gating hooks around the incidents service."""

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
    ctx.register_hook("pre_tool_call", pre_tool_call)
