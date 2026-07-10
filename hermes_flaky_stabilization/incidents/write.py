"""NET-NEW tracker write-back: ``jira_create_incident`` (plan D7, off by default).

No legacy code pushed to Jira — the legacy client was GET-only. This module
adds the pipeline's one endpoint, mechanically guarded (plan D6):

* config-gated: refuses unless ``jira.enable_write`` is true (default false);
* check_fn-hidden without ``JIRA_API_TOKEN`` (wired in :func:`register_tool`);
* approval-escalated via the ``pre_tool_call`` hook (orchestrator.hooks);
* PII-gated in the handler (deterministic, not advisory): every referenced
  evidence path must pass the scanner gate ``clean && complete`` within this
  same call, and every outbound text field passes ``redaction.redact_text``
  before it is sent;
* error envelopes never echo the request body.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..pii import redaction
from . import config as config_mod

logger = logging.getLogger(__name__)

TOOL_NAME = "jira_create_incident"

CREATE_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Create a Jira incident from a structured, PII-gated ticket. Off by "
        "default: requires jira.enable_write in the plugin config AND the "
        "JIRA_API_TOKEN credential, and every call is approval-escalated. All "
        "outbound text is PII-redacted; any referenced evidence file must pass "
        "the validate_no_pii gate (clean and complete) or the call refuses."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Issue summary line."},
            "body": {"type": "string", "description": "Issue description (plain text)."},
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional labels to set on the issue.",
            },
            "severity": {
                "type": "string",
                "description": "Optional severity note (recorded in the description).",
            },
            "evidence_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Local evidence files referenced by this ticket; each must "
                    "pass the PII gate before anything is sent."
                ),
            },
        },
        "required": ["title", "body"],
    },
}


def _error(message: str, remediation: str = "", **extra: Any) -> str:
    payload = {"success": False, "error": message, "remediation": remediation, **extra}
    return json.dumps(payload, ensure_ascii=False)


def _adf(text: str) -> dict[str, Any]:
    """Wrap plain text in the minimal Atlassian Document Format body."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text or " "}]}
        ],
    }


def create_incident(client, ticket: dict[str, Any]) -> dict[str, Any]:
    """POST the (already gated/redacted) *ticket* via *client*.

    ``project_key`` and ``issue_type`` come from the *ticket* that
    :func:`build_ticket` assembled, not from the config. Returns
    ``{"created": True, "key", "url"}``; raises on transport errors (the
    handler shapes those into envelopes that never echo the body).
    """
    fields: dict[str, Any] = {
        "project": {"key": ticket["project_key"]},
        "issuetype": {"name": ticket["issue_type"]},
        "summary": ticket["title"],
        "description": _adf(ticket["body"]),
    }
    if ticket.get("labels"):
        fields["labels"] = list(ticket["labels"])
    response = client.create_issue(fields)
    key = response.get("key", "")
    url = f"{client.base_url}/browse/{key}" if key else ""
    return {"created": True, "key": key, "url": url}


def _egress_clean(text: str) -> str:
    """redact_text then the scanner's mask_pii egress canary (D6.4b).

    ``redact_text`` misses PII classes the plugin's own scanner detects
    (validated cards/IBANs, space-separated SSNs, …). The pipeline's tracker
    stage runs this same second pass; the direct ``jira_create_incident`` tool
    must not be the weaker path, so it runs it here too. ``mask_pii`` never
    raises on the model-facing path.
    """
    from ..pii.scanner import mask_pii

    return mask_pii(redaction.redact_text(text))


def build_ticket(args: dict[str, Any], jira_section: dict[str, Any]) -> dict[str, Any]:
    """Assemble the outbound ticket with every text field redacted (D6.4b)."""
    title = _egress_clean(str(args.get("title") or "").strip())
    body = _egress_clean(str(args.get("body") or "").strip())
    severity = str(args.get("severity") or "").strip()
    if severity:
        body = f"{body}\n\nSeverity: {_egress_clean(severity)}"
    labels = [
        _egress_clean(str(label)) for label in (args.get("labels") or [])
        if str(label).strip()
    ]
    return {
        "title": title,
        "body": body,
        "labels": labels,
        "project_key": str(jira_section.get("project_key") or "INC"),
        "issue_type": str(jira_section.get("issue_type") or "Bug"),
    }


def handle_create_incident(args: dict[str, Any], **kwargs: Any) -> str:
    """Tool handler. Always returns a JSON string; never echoes the request body."""
    from pathlib import Path

    from .. import config as unified_config
    from ..orchestrator import gate

    if not isinstance(args, dict):
        return _error("Invalid arguments: expected an object.",
                      'Call with {"title": ..., "body": ...}.')
    title = args.get("title")
    body = args.get("body")
    if not isinstance(title, str) or not title.strip():
        return _error("Missing required argument 'title'.",
                      "Pass the issue summary line as 'title'.")
    if not isinstance(body, str) or not body.strip():
        return _error("Missing required argument 'body'.",
                      "Pass the issue description as 'body'.")

    home = config_mod.resolve_hermes_home()
    cfg = unified_config.load_config(Path(home) / "flaky-stabilization")
    jira_section = cfg.get("jira") if isinstance(cfg.get("jira"), dict) else {}

    # Gate 1 (config): write-back is off by default (plan D7).
    if not jira_section.get("enable_write"):
        return _error(
            "tracker_write_disabled",
            "Set jira.enable_write=true in the flaky-stabilization config to "
            "allow creating Jira incidents; until then, file the returned "
            "ticket body manually.",
        )

    # Gate 2 (PII, deterministic — plan D6.4a): every referenced evidence path
    # must be clean AND complete in this same call.
    gate_result = gate.assert_evidence_clean(args.get("evidence_paths"))
    if not gate_result.ok:
        return _error(
            "pii_gate_failed",
            "Scrub or drop the flagged evidence, then retry; nothing was sent.",
            findings_summary=gate_result.findings_summary,
            dirty_paths=gate_result.dirty_paths,
            incomplete_paths=gate_result.incomplete_paths,
            gate_errors=gate_result.errors,
        )

    token = config_mod.resolve_token()
    incidents_config = config_mod.IncidentsConfig.from_dict(config_mod.load_config(home))
    if not token or not incidents_config.base_url():
        return _error(
            "jira_credentials_missing",
            "Set JIRA_API_TOKEN (env) and jira.base_url (config) to enable "
            "tracker write-back.",
        )

    # Gate 3 (redaction — plan D6.4b): every outbound field is redacted.
    ticket = build_ticket(args, jira_section)

    try:
        client = config_mod.build_client(incidents_config, token=token)
        result = create_incident(client, ticket)
    except Exception as exc:
        # Never echo the request body or the raw response; the client already
        # strips response bodies from its errors.
        logger.warning("%s failed: %s", TOOL_NAME, type(exc).__name__)
        return _error(
            f"tracker_write_failed ({type(exc).__name__})",
            "Check Jira credentials, base URL, project key, and permissions.",
        )
    return json.dumps({"success": True, **result}, ensure_ascii=False)


def check_fn() -> bool:
    """Hide the tool without a token or with write-back disabled (plan §6.2)."""
    try:
        if not config_mod.resolve_token():
            return False
        from pathlib import Path

        from .. import config as unified_config

        home = config_mod.resolve_hermes_home()
        cfg = unified_config.load_config(Path(home) / "flaky-stabilization")
        jira_section = cfg.get("jira") if isinstance(cfg.get("jira"), dict) else {}
        return bool(jira_section.get("enable_write"))
    except Exception:
        return False


def register_tool(ctx) -> None:
    ctx.register_tool(
        name=TOOL_NAME,
        toolset="jira_incidents",
        schema=CREATE_SCHEMA,
        handler=handle_create_incident,
        check_fn=check_fn,
    )
