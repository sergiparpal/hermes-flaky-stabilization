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
                           ``heal_flaky_test`` with ``mode="pr"``, for every
                           ``jira_create_incident`` call, and for
                           ``stabilize_test_failure`` whenever the run could
                           reach an external write — ``mode="pr"`` requested,
                           or the tracker write is live (``jira.enable_write``
                           on AND ``JIRA_API_TOKEN`` set; a failed config read
                           escalates, fail closed). The pipeline calls its
                           tracker stage as a plain function, so this hook is
                           the D6.3 approval gate for that path too. The host
                           treats the directive fail-closed (error/deny/timeout
                           ⇒ blocked). Dict inspection plus, for stabilize
                           only, one local config-file read — never network
                           I/O on the hot path.
* ``pre_approval_request`` / ``post_approval_response`` — the healer's audit
                           observers (registered by the healer stage itself).

Hook callbacks must accept ``**kwargs`` and never raise (the host swallows
exceptions to WARNING, but a broken hook still wastes a turn's error budget).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

RULE_KEY_PR = "flaky_stab_pr"
RULE_KEY_TRACKER_WRITE = "flaky_stab_tracker_write"
RULE_KEY_STABILIZE = "flaky_stab_stabilize_pipeline"


def _pipeline_heal_mode(args: dict | None) -> str:
    """The heal mode a stabilize run will *actually* use.

    Resolved exactly as ``Pipeline._heal_branch`` does — the explicit ``mode``
    argument, else the configured ``pipeline.default_heal_mode``, else
    ``"suggest"`` — so the escalation decision matches the run's real behaviour.
    Without this the hook only saw ``args["mode"]`` and a config default of
    ``"pr"`` would open a PR with no approval prompt (the pipeline calls the
    heal stage as a plain function, so no per-tool escalation fires either).
    A config-read failure fails closed (treated as ``"pr"``), mirroring
    :func:`_jira_write_live`.
    """
    mode = str((args or {}).get("mode") or "").strip().lower()
    if mode:
        return mode
    try:
        from .. import config as unified_config

        pipeline_cfg = unified_config.load_config().get("pipeline") or {}
        return str(pipeline_cfg.get("default_heal_mode") or "suggest").strip().lower()
    except Exception:
        logger.warning(
            "unified config read failed in pre_tool_call; treating stabilize "
            "heal mode as 'pr' (fail closed)", exc_info=True,
        )
        return "pr"


def _jira_write_live() -> bool:
    """True when a pipeline run could reach the live Jira tracker.

    The tracker write needs both ``jira.enable_write`` (config) and
    ``JIRA_API_TOKEN`` (env); without the token no write can happen, so no
    escalation is needed. A config read failure counts as live — fail closed,
    like the host treats a broken approval directive.
    """
    if not os.environ.get("JIRA_API_TOKEN"):
        return False
    try:
        from .. import config as unified_config

        jira_cfg = unified_config.load_config().get("jira") or {}
        return bool(jira_cfg.get("enable_write"))
    except Exception:
        logger.warning(
            "unified config read failed in pre_tool_call; escalating "
            "stabilize_test_failure (fail closed)", exc_info=True,
        )
        return True


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Escalate the sensitive calls to the human approval gate (D6.3)."""
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
    if tool_name == "stabilize_test_failure":
        # The orchestrator invokes its tracker/healer stages as plain function
        # calls (no dispatch_tool), so the per-tool escalations above never
        # fire inside a pipeline run — this rule is the D6.3 gate for the
        # whole run whenever it could produce an external write.
        reasons = []
        mode = _pipeline_heal_mode(args)
        if mode == "pr":
            reasons.append("open a pull request for a healed test (mode='pr')")
        if _jira_write_live():
            reasons.append(
                "create a Jira issue on the bug branch (jira.enable_write is "
                "on and JIRA_API_TOKEN is set)"
            )
        if reasons:
            return {
                "action": "approve",
                "message": (
                    "stabilize_test_failure may " + " and ".join(reasons) +
                    ". The pipeline calls these stages directly, so this is "
                    "the approval gate for the run. Allow?"
                ),
                "rule_key": RULE_KEY_STABILIZE,
            }
    return None


def register(ctx, *, incidents_service) -> None:
    """Wire the lifecycle + gating hooks around the incidents service."""

    def on_pre_llm_call(user_message: str = "", **kwargs):
        try:
            return incidents_service.llm_context(user_message, **kwargs)
        except Exception:  # defensive: the service already guards internally
            logger.debug("incident context injection failed", exc_info=True)
            return None

    def on_session_start(session_id: str = "", **kwargs):
        try:
            incidents_service.on_session_start(session_id, **kwargs)
        except Exception:
            logger.debug("session-start sync trigger failed", exc_info=True)
        return None

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_tool_call", pre_tool_call)
