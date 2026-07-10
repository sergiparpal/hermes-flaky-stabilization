"""``stabilize_test_failure`` — the pipeline entry (plan §6.3, normative).

The orchestrator that did not exist before unification: plain Python control
flow calling the stage functions directly (no internal tool dispatch — only
the healer's git/PR steps go through ``ctx.dispatch_tool``, inside the healer,
so they keep the host approval pipeline).

    1. Evidence: a given log, or fetch_ci_logs when build_id+repo+token exist.
    2. History:  test_failure_lookup + is_flaky for a given test_id.
    3. Triage:   classify with (a) the fixed history enrichment and (b) the
                 NET-NEW incident enrichment — a redacted local-FTS lookup of
                 the incidents index seeds the classifier's prior-hint block
                 (the incidents→triage feedback loop, plan D9).
    4. FORK on category:
       a. heal categories (default flaky|timeout) → healer; a stable heal
          writes the burn-in outcome back into history.db as a synthetic run
          (the heal→history feedback loop, plan D9), and PR mode returns the
          host-approved PR.
       b. anything else → bug branch: improve_bug_report →
          find_duplicate_incidents → PII gate → jira_create_incident when
          enabled, else the redacted ticket body (outcome ``ticket_ready``).
    5. A ``pipeline_runs`` ledger row records every run.

Every stage result is present in every outcome (``{"skipped": <reason>}``
when a branch was not taken) so the model and tests can always see why.

Stage callables are injected (tests fake any subset); the defaults bind the
real in-package implementations.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# build_id is untrusted input that ends up in a spool *filename*; anything
# outside this class (path separators above all) is replaced with "-".
_SAFE_BUILD_ID_RE = re.compile(r"[^A-Za-z0-9._-]")

STAGE_KEYS = (
    "evidence", "history", "detective", "triage",
    "healer", "bugreport", "dedup", "pii", "tracker",
)

OUTCOME_HEALED = "healed"
OUTCOME_PR_OPENED = "pr_opened"
OUTCOME_TICKET_CREATED = "ticket_created"
OUTCOME_TICKET_READY = "ticket_ready"
OUTCOME_NEEDS_ATTENTION = "needs_attention"

_INCIDENT_HINT_LIMIT = 3

STABILIZE_SCHEMA = {
    "name": "stabilize_test_failure",
    "description": (
        "Run the full flaky-test stabilization pipeline on one failure: look up "
        "the test's failure history and flaky verdict, triage the CI log into a "
        "category, then either heal the flaky test in a sandbox (optionally "
        "opening a PR) or draft a structured, PII-gated bug ticket with local "
        "duplicate-incident candidates. Returns a JSON envelope with every "
        "stage's result and an overall outcome: healed, pr_opened, "
        "ticket_created, ticket_ready, or needs_attention."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "log_url_or_path": {
                "type": "string",
                "description": "CI log to triage (local path or https URL).",
            },
            "test_id": {
                "type": "string",
                "description": "Failing test id ('classname::name', 'file::name', or bare name).",
            },
            "repo_dir": {
                "type": "string",
                "description": "Project checkout for the healer branch (required to heal).",
            },
            "trace_path": {
                "type": "string",
                "description": "Optional Playwright trace.zip for healer diagnosis.",
            },
            "build_id": {
                "type": "string",
                "description": "CI run id — with 'repo', fetches the log via GitHub Actions.",
            },
            "repo": {
                "type": "string",
                "description": "GitHub 'owner/name' for build_id log fetching.",
            },
            "project": {
                "type": "string",
                "description": "Project key for triage pattern learning.",
            },
            "mode": {
                "type": "string",
                "enum": ["suggest", "pr"],
                "description": "Healer mode; 'pr' needs approval (default from config).",
            },
            "evidence_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Extra evidence files for the bug branch's PII gate.",
            },
        },
        "required": [],
    },
}


@dataclass
class Stages:
    """Injected stage callables (tests fake any subset)."""

    fetch_ci_logs: Callable[[dict], str] | None = None
    history_lookup: Callable[[str], dict] | None = None
    is_flaky: Callable[[str], dict] | None = None
    triage: Callable[..., str] | None = None
    heal: Callable[[dict], str] | None = None
    bugreport: Callable[[dict], str] | None = None
    dedup: Callable[[str], dict] | None = None
    gate: Callable[[list], Any] | None = None
    tracker: Callable[[dict], str] | None = None
    ingest_synthetic: Callable[..., int] | None = None
    incident_search: Callable[[str], list] | None = None


@dataclass
class Pipeline:
    """The orchestrator; construct via :func:`build_pipeline` for real wiring."""

    stages: Stages
    config: dict[str, Any] = field(default_factory=dict)
    record_run: Callable[[dict], None] | None = None

    # -- public entry -----------------------------------------------------

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        args = args if isinstance(args, dict) else {}
        results: dict[str, Any] = {key: {"skipped": "not reached"} for key in STAGE_KEYS}
        notes: list[str] = []

        pipeline_cfg = self.config.get("pipeline") or {}
        heal_categories = pipeline_cfg.get("heal_categories") or ["flaky", "timeout"]

        log_path = self._evidence(args, results, notes)
        test_id = str(args.get("test_id") or "").strip()
        self._history(test_id, results)
        category = self._triage(args, log_path, test_id, results, notes)

        outcome = OUTCOME_NEEDS_ATTENTION
        if category is None and self._verdict_is_flaky(results["detective"]):
            category = "flaky"
            notes.append("no triage category; forked on the detective verdict")

        # The branch is decided HERE, at the fork, and recorded as such — the
        # ledger must not reverse-engineer it from stage-result shapes (a heal
        # branch that skipped the healer would otherwise record branch=NULL).
        branch: str | None = None
        if category in heal_categories:
            branch = "heal"
            for key in ("bugreport", "dedup", "pii", "tracker"):
                results[key] = {"skipped": f"heal branch taken (category {category!r})"}
            outcome = self._heal_branch(args, test_id, results, notes)
        elif category is not None:
            branch = "bug"
            results["healer"] = {"skipped": f"bug branch taken (category {category!r})"}
            outcome = self._bug_branch(args, log_path, test_id, results, notes)
        else:
            results["healer"] = {"skipped": "no category and no flaky verdict"}
            for key in ("bugreport", "dedup", "pii", "tracker"):
                results[key] = {"skipped": "no category and no flaky verdict"}
            notes.append("nothing to act on: supply a log and/or a known test_id")

        envelope = {
            "success": True,
            "outcome": outcome,
            "stage_results": results,
            "notes": notes,
        }
        self._ledger(args, category, branch, outcome, results, time.monotonic() - started)
        return envelope

    # -- stage steps --------------------------------------------------------

    def _evidence(self, args, results, notes) -> str | None:
        log = str(args.get("log_url_or_path") or "").strip()
        if log:
            results["evidence"] = {"source": "given_log", "log": log}
            return log
        build_id = str(args.get("build_id") or "").strip()
        repo = str(args.get("repo") or "").strip()
        if build_id and repo and self.stages.fetch_ci_logs is not None:
            try:
                raw = self.stages.fetch_ci_logs({"build_id": build_id, "repo": repo})
                data = json.loads(raw) if isinstance(raw, str) else raw
            except Exception as exc:
                results["evidence"] = {"error": f"fetch_ci_logs failed ({type(exc).__name__})"}
                return None
            if isinstance(data, dict) and data.get("filtered_log"):
                try:
                    # Inside the stage's error envelope: an unwritable spool dir
                    # (or any other spool failure) is an evidence-stage error,
                    # not a run abort that loses every result and the ledger row.
                    path = self._spool_log(data["filtered_log"], build_id)
                except Exception as exc:
                    results["evidence"] = {
                        "error": f"evidence spool failed ({type(exc).__name__})",
                    }
                    return None
                results["evidence"] = {
                    "source": "fetch_ci_logs", "log": str(path),
                    "bytes_filtered": data.get("bytes_filtered"),
                }
                return str(path)
            results["evidence"] = {
                "error": (data or {}).get("error", "fetch_ci_logs returned no log")
                if isinstance(data, dict) else "fetch_ci_logs returned no log",
            }
            return None
        results["evidence"] = {"skipped": "no log given (and no build_id+repo+token)"}
        return None

    def _spool_log(self, text: str, build_id: str) -> Path:
        from .. import paths

        spool = paths.get_data_dir() / "pipeline-evidence"
        spool.mkdir(parents=True, exist_ok=True)
        # build_id is untrusted ("123/456" would escape the spool dir or just
        # fail on a missing subdirectory); sanitise before it names a file.
        safe_id = _SAFE_BUILD_ID_RE.sub("-", build_id) or "unknown"
        path = spool / f"ci-log-{safe_id}.log"
        path.write_text(text, encoding="utf-8")
        from ..paths import restrict_file

        restrict_file(path)
        return path

    def _history(self, test_id: str, results) -> None:
        if not test_id:
            results["history"] = {"skipped": "no test_id given"}
            results["detective"] = {"skipped": "no test_id given"}
            return
        if self.stages.history_lookup is not None:
            try:
                results["history"] = self.stages.history_lookup(test_id)
            except Exception as exc:
                results["history"] = {"error": f"history lookup failed ({type(exc).__name__})"}
        else:
            results["history"] = {"skipped": "history stage unavailable"}
        if self.stages.is_flaky is not None:
            try:
                results["detective"] = self.stages.is_flaky(test_id)
            except Exception as exc:
                results["detective"] = {"error": f"is_flaky failed ({type(exc).__name__})"}
        else:
            results["detective"] = {"skipped": "detective stage unavailable"}

    @staticmethod
    def _verdict_is_flaky(detective_result) -> bool:
        return isinstance(detective_result, dict) and detective_result.get("is_flaky") is True

    def _incident_context(self, log_path: str | None, test_id: str,
                          notes: list[str]) -> list | None:
        """The NET-NEW incidents→triage feedback seed (redacted, local-only).

        Failures here must never be silent: an errored lookup is otherwise
        indistinguishable from "no similar incidents", so both fallbacks log a
        warning and leave a note in the run.
        """
        if self.stages.incident_search is None:
            return None
        query = test_id
        if log_path and not str(log_path).startswith("http"):
            try:
                raw = Path(log_path).read_text(encoding="utf-8", errors="replace")
                from ..triage import enrichment as triage_enrichment
                from ..triage import prefilter

                excerpt, _stats = prefilter.prefilter(raw)
                query = triage_enrichment.top_signal_line(excerpt) or query
            except Exception as exc:
                logger.warning("incident-context query derivation failed", exc_info=True)
                notes.append(
                    f"incident-context query derivation failed "
                    f"({type(exc).__name__}) — fell back to the bare test id"
                )
        if not query:
            return None
        try:
            rows = self.stages.incident_search(query)
        except Exception as exc:
            logger.warning("incident-context lookup failed", exc_info=True)
            notes.append(
                f"incident-context lookup failed ({type(exc).__name__}) — "
                f"triage ran without incident hints"
            )
            return None
        return rows or None

    def _triage(self, args, log_path, test_id, results, notes) -> str | None:
        if not log_path:
            results["triage"] = {"skipped": "no log evidence to triage"}
            return None
        if self.stages.triage is None:
            results["triage"] = {"skipped": "triage stage unavailable"}
            return None
        incident_context = self._incident_context(log_path, test_id, notes)
        call: dict[str, Any] = {"log_url_or_path": log_path}
        if args.get("project"):
            call["project"] = args["project"]
        try:
            raw = self.stages.triage(call, incident_context=incident_context)
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            results["triage"] = {"error": f"triage failed ({type(exc).__name__})"}
            return None
        results["triage"] = data
        if not isinstance(data, dict) or not data.get("success"):
            return None
        return data.get("category")

    # -- heal branch ---------------------------------------------------------

    def _heal_branch(self, args, test_id, results, notes) -> str:
        repo_dir = str(args.get("repo_dir") or "").strip()
        if not repo_dir or not test_id:
            results["healer"] = {"skipped": "healer needs repo_dir and test_id"}
            notes.append("flaky category but no repo_dir/test_id — cannot heal")
            return OUTCOME_NEEDS_ATTENTION
        if self.stages.heal is None:
            results["healer"] = {"skipped": "healer stage unavailable"}
            return OUTCOME_NEEDS_ATTENTION
        pipeline_cfg = self.config.get("pipeline") or {}
        mode = str(args.get("mode") or pipeline_cfg.get("default_heal_mode") or "suggest")
        call = {"repo_dir": repo_dir, "test_id": test_id, "mode": mode}
        for optional in ("trace_path", "build_id", "repo"):
            if args.get(optional):
                call[optional] = args[optional]
        try:
            raw = self.stages.heal(call)
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            results["healer"] = {"error": f"heal failed ({type(exc).__name__})"}
            return OUTCOME_NEEDS_ATTENTION
        results["healer"] = data
        if not isinstance(data, dict):
            return OUTCOME_NEEDS_ATTENTION
        report = data.get("report") or {}
        if report.get("stable"):
            self._feed_back_burnin(test_id, report, results, notes)
            if data.get("pr"):
                return OUTCOME_PR_OPENED
            return OUTCOME_HEALED
        notes.append("heal did not reach a stable burn-in")
        return OUTCOME_NEEDS_ATTENTION

    def _feed_back_burnin(self, test_id, report, results, notes) -> None:
        """Loop 1 (plan D9): a stable heal writes its burn-in outcome back into
        history.db as a synthetic run, so the next detective scan sees the
        recovery."""
        if self.stages.ingest_synthetic is None:
            return
        # The healer's real envelope is report["burnin"] (HealReport.to_dict)
        # with the sandbox aggregate's "runs"/"passed" keys. Only stable heals
        # reach this point, where passed == runs; prefer "passed" (what
        # actually went green) with "runs" as the robustness fallback.
        burnin = report.get("burnin") or {}
        repeats = int(burnin.get("passed") or burnin.get("runs") or 0)
        classname, name = None, test_id
        if "::" in test_id:
            classname, name = test_id.rsplit("::", 1)
        cases = [{
            "classname": classname,
            "name": name,
            "file_path": report.get("test_id") or test_id,
            "status": "passed",
        }] * max(1, repeats)
        try:
            run_id = self.stages.ingest_synthetic(cases=cases)
            notes.append(
                f"burn-in outcome fed back into test history (run_id={run_id}, "
                f"{len(cases)} green case row(s))"
            )
        except Exception as exc:
            notes.append(f"burn-in feedback failed ({type(exc).__name__})")

    # -- bug branch ------------------------------------------------------------

    def _bug_branch(self, args, log_path, test_id, results, notes) -> str:
        report = self._bugreport(args, log_path, test_id, results)
        if report is None:
            return OUTCOME_NEEDS_ATTENTION

        summary_query = f"{report.get('title', '')} {report.get('summary', '')}".strip()
        if self.stages.dedup is not None and summary_query:
            try:
                results["dedup"] = self.stages.dedup(summary_query)
            except Exception as exc:
                results["dedup"] = {"error": f"dedup failed ({type(exc).__name__})"}
        else:
            results["dedup"] = {"skipped": "no dedup stage or empty summary"}

        evidence_paths = self._coerce_evidence_paths(args.get("evidence_paths"),
                                                     results, notes)
        if evidence_paths is None:
            return OUTCOME_NEEDS_ATTENTION
        if log_path and not str(log_path).startswith("http"):
            evidence_paths.append(str(log_path))

        pipeline_cfg = self.config.get("pipeline") or {}
        if pipeline_cfg.get("require_pii_gate", True):
            if self.stages.gate is None:
                # Fail CLOSED, mirroring assert_evidence_clean's scan-error
                # policy: a required gate that cannot run must block the
                # tracker, not wave the evidence through.
                results["pii"] = {
                    "ok": False,
                    "error": "pii gate required by config but unavailable (fail closed)",
                }
                results["tracker"] = {"skipped": "pii_gate_unavailable — nothing sent"}
                notes.append("PII gate required but unavailable — failing closed")
                return OUTCOME_NEEDS_ATTENTION
            gate_result = self.stages.gate(evidence_paths)
            gate_dict = gate_result.to_dict() if hasattr(gate_result, "to_dict") else gate_result
            results["pii"] = gate_dict
            if not gate_dict.get("ok"):
                results["tracker"] = {"skipped": "pii_gate_failed — nothing sent"}
                notes.append("PII gate failed: scrub or drop the flagged evidence")
                return OUTCOME_NEEDS_ATTENTION
        else:
            results["pii"] = {"skipped": "gate disabled by config"}

        return self._tracker(report, evidence_paths, results, notes)

    @staticmethod
    def _coerce_evidence_paths(raw: Any, results, notes) -> list[str] | None:
        """Validate the untrusted ``evidence_paths`` argument.

        The slash command (and models) routinely pass a lone string — iterating
        it would char-split into single-character "paths" and send the gate
        walking the filesystem root; a non-iterable would raise ``TypeError``
        out of ``run()`` and lose the ledger row. A lone string is one path;
        any other non-list shape is a clean stage error (``None`` return —
        the caller ends the run as ``needs_attention`` with the row written).
        """
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw] if raw.strip() else []
        if isinstance(raw, (list, tuple)):
            return [str(p) for p in raw]
        results["pii"] = {
            "ok": False,
            "error": (f"invalid evidence_paths type ({type(raw).__name__}); "
                      "expected a list of path strings"),
        }
        results["tracker"] = {"skipped": "invalid evidence_paths — nothing sent"}
        notes.append("evidence_paths must be a list of file paths (or one path string)")
        return None

    def _bugreport(self, args, log_path, test_id, results) -> dict | None:
        if self.stages.bugreport is None:
            results["bugreport"] = {"skipped": "bugreport stage unavailable"}
            return None
        raw_text = self._raw_evidence_text(args, log_path, test_id, results)
        try:
            raw = self.stages.bugreport({"raw_text": raw_text, "format": "json"})
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            results["bugreport"] = {"error": f"bug report failed ({type(exc).__name__})"}
            return None
        if not isinstance(data, dict) or data.get("error"):
            results["bugreport"] = data if isinstance(data, dict) else {"error": "bad report"}
            return None
        results["bugreport"] = data
        return data

    def _raw_evidence_text(self, args, log_path, test_id, results) -> str:
        parts: list[str] = []
        triage_result = results.get("triage")
        if isinstance(triage_result, dict) and triage_result.get("success"):
            parts.append(
                f"CI triage: category={triage_result.get('category')} "
                f"({triage_result.get('classification_method')}, "
                f"confidence {triage_result.get('confidence')}): "
                f"{triage_result.get('summary', '')}"
            )
            for line in triage_result.get("evidence") or []:
                parts.append(f"evidence: {line}")
        if test_id:
            parts.append(f"Failing test: {test_id}")
            history = results.get("history")
            if isinstance(history, dict) and history.get("failure_count"):
                parts.append(
                    f"History: {history.get('failure_count')} failure(s) over "
                    f"{history.get('total_runs')} run(s); last at "
                    f"{history.get('last_failure_at')}"
                )
        if log_path:
            parts.append(f"CI log: {log_path}")
        return "\n".join(parts) or "CI pipeline failure (no further evidence collected)"

    @staticmethod
    def _egress_mask(text: str) -> tuple[str, bool]:
        """Egress canary (D6.4b hardening) on outbound ticket text.

        ``redact_text`` misses PII classes the plugin's own scanner detects
        (validated IBANs, space-separated SSNs, …), so after redaction the
        detectors run once more over the assembled text and any remaining
        finding is masked in place via its span. Returns ``(text, fired)``.
        """
        from ..pii.scanner import mask_pii

        masked = mask_pii(text)
        return masked, masked != text

    def _tracker(self, report, evidence_paths, results, notes) -> str:
        from ..pii import redaction

        # D7: the TITLE is outbound text exactly like the body — redact it on
        # this side too (the enabled path's build_ticket re-redacts internally,
        # but the ticket_ready title is hand-pasted into Jira as returned).
        title, title_fired = self._egress_mask(
            redaction.redact_text(str(report.get("title") or "")))
        body, body_fired = self._egress_mask(self._ticket_body(report))
        egress_fired = title_fired or body_fired
        if egress_fired:
            notes.append(
                "egress canary fired: PII remained after redaction and was "
                "masked in the outbound ticket text"
            )
        jira_cfg = self.config.get("jira") or {}
        if not jira_cfg.get("enable_write") or self.stages.tracker is None:
            ticket: dict[str, Any] = {"title": title, "body": body}
            if egress_fired:
                ticket["egress_masked"] = True
            results["tracker"] = {
                "skipped": "jira.enable_write is off",
                "ticket": ticket,
            }
            notes.append("tracker write disabled — returning the ticket body to file manually")
            return OUTCOME_TICKET_READY
        try:
            raw = self.stages.tracker({
                "title": title,
                "body": body,
                "severity": report.get("severity", ""),
                "evidence_paths": evidence_paths,
            })
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            results["tracker"] = {"error": f"tracker write failed ({type(exc).__name__})"}
            return OUTCOME_NEEDS_ATTENTION
        results["tracker"] = data
        if isinstance(data, dict) and data.get("created"):
            return OUTCOME_TICKET_CREATED
        notes.append("tracker write did not create an issue")
        return OUTCOME_NEEDS_ATTENTION

    @staticmethod
    def _ticket_body(report: dict) -> str:
        from ..pii import redaction

        lines = [report.get("summary", "")]
        steps = report.get("reproduction_steps") or []
        if steps:
            lines.append("")
            lines.append("Reproduction steps:")
            lines.extend(f"{i}. {step}" for i, step in enumerate(steps, 1))
        for label, key in (("Expected", "expected_behavior"),
                           ("Actual", "actual_behavior"),
                           ("Severity", "severity"),
                           ("Severity rationale", "severity_rationale")):
            value = report.get(key)
            if value:
                lines.append(f"{label}: {value}")
        missing = report.get("missing_evidence") or []
        if missing:
            lines.append("Missing evidence: " + "; ".join(str(m) for m in missing))
        return redaction.redact_text("\n".join(lines))

    # -- ledger -----------------------------------------------------------------

    def _ledger(self, args, category, branch, outcome, results, duration_s) -> None:
        """Record the run; *branch* is the fork decision made in :meth:`run`
        (never reverse-engineered from stage-result shapes, which would record
        NULL for exactly the failed runs one queries the ledger for)."""
        if self.record_run is None:
            return
        try:
            self.record_run({
                "trigger": "tool",
                "test_id": args.get("test_id"),
                "project": args.get("project"),
                "triage_category": category,
                "branch": branch,
                "outcome": outcome,
                "stage_results_json": json.dumps(results, ensure_ascii=False, default=str),
                "duration_s": round(duration_s, 3),
            })
        except Exception:
            logger.warning("pipeline_runs ledger write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Real wiring
# ---------------------------------------------------------------------------


def _record_run_state_db(row: dict[str, Any]) -> None:
    from contextlib import closing
    from datetime import UTC, datetime

    from ..storage import state

    with closing(state.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO pipeline_runs (ts, trigger, test_id, project, triage_category,"
            " branch, outcome, stage_results_json, duration_s)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                row.get("trigger", "tool"),
                row.get("test_id"),
                row.get("project"),
                row.get("triage_category"),
                row.get("branch"),
                row.get("outcome", ""),
                row.get("stage_results_json", "{}"),
                row.get("duration_s"),
            ),
        )


def build_pipeline(ctx, incidents_service) -> Pipeline:
    """Bind the real stage implementations (direct in-package calls — the only
    dispatch_tool use stays inside the healer's git/PR flow, plan D3/D6.2)."""
    import functools

    from .. import config as unified_config
    from ..bugreport import handler as bugreport_handler
    from ..detective import _handle_is_flaky
    from ..healer import handlers as healer_handlers
    from ..history import _handle_test_failure_lookup
    from ..history import ingest as history_ingest
    from ..history import storage as history_storage
    from ..incidents import write as incidents_write
    from ..pii import redaction
    from ..triage import handlers as triage_handlers
    from . import dedup as dedup_mod
    from . import gate as gate_mod

    cfg = unified_config.load_config()

    def history_lookup(test_id: str) -> dict:
        return json.loads(_handle_test_failure_lookup({"test_id": test_id}))

    def is_flaky(test_id: str) -> dict:
        return json.loads(_handle_is_flaky({"test_id": test_id}))

    def triage_stage(call: dict, incident_context=None) -> str:
        from ..triage import _enable_enrichment, _resolve_home

        return triage_handlers.triage_pipeline_failure(
            call,
            llm=getattr(ctx, "llm", None),
            hermes_home=_resolve_home(),
            enable_enrichment=_enable_enrichment(),
            incident_context=incident_context,
        )

    def incident_search(query: str) -> list:
        incidents_service.ensure_initialized()
        store = incidents_service.store
        if store is None:
            return []
        rows = store.search(query, limit=_INCIDENT_HINT_LIMIT)
        return [
            {
                key: value
                for key, value in redaction.redact_incident(
                    row, fields=("key", "summary", "status")
                ).items()
                if key in ("key", "summary", "status")
            }
            for row in rows
        ]

    def dedup_stage(summary: str) -> dict:
        incidents_service.ensure_initialized()
        return dedup_mod.find_duplicate_incidents(incidents_service.store, summary)

    def ingest_synthetic(**kwargs) -> int:
        conn = history_storage.get_connection()
        return history_ingest.ingest_synthetic_run(conn, **kwargs)

    return Pipeline(
        stages=Stages(
            fetch_ci_logs=functools.partial(healer_handlers.fetch_ci_logs, ctx=ctx),
            history_lookup=history_lookup,
            is_flaky=is_flaky,
            triage=triage_stage,
            heal=functools.partial(healer_handlers.heal_flaky_test, ctx=ctx),
            bugreport=bugreport_handler.make_handler(ctx),
            dedup=dedup_stage,
            gate=gate_mod.assert_evidence_clean,
            tracker=incidents_write.handle_create_incident,
            ingest_synthetic=ingest_synthetic,
            incident_search=incident_search,
        ),
        config=cfg,
        record_run=_record_run_state_db,
    )


def make_tool_handler(ctx, incidents_service):
    """The registered ``stabilize_test_failure`` handler (JSON string contract)."""

    def handler(args, **kwargs) -> str:
        try:
            pipeline = build_pipeline(ctx, incidents_service)
            return json.dumps(pipeline.run(args if isinstance(args, dict) else {}),
                              ensure_ascii=False)
        except Exception as exc:
            logger.exception("stabilize_test_failure crashed")
            return json.dumps({
                "success": False,
                "error": f"internal error ({type(exc).__name__})",
                "remediation": "See the agent log for details.",
            })
    return handler


def make_command(ctx, incidents_service):
    """The ``/stabilize`` slash command: `<log-or-test-id> [key=value ...]`."""
    import os
    import shlex

    def command(raw_args=None, **kwargs) -> str:
        try:
            tokens = shlex.split(str(raw_args or "").strip())
        except ValueError:
            tokens = str(raw_args or "").split()
        if not tokens:
            return ("usage: /stabilize <ci-log-path-or-test-id> "
                    "[repo_dir=…] [mode=suggest|pr] [project=…]")
        args: dict[str, Any] = {}
        head = tokens[0]
        if head.startswith("http"):
            args["log_url_or_path"] = head
        elif "::" in head and not os.path.exists(head):
            # The documented `file_path::name` test-id form: a '::'-containing
            # head that is not an actual file on disk is a test id, even when
            # it carries a path separator (tests/login.spec.ts::checkout).
            args["test_id"] = head
        elif os.path.sep in head or os.path.exists(head):
            args["log_url_or_path"] = head
        else:
            args["test_id"] = head
        for token in tokens[1:]:
            if "=" in token:
                key, value = token.split("=", 1)
                key, value = key.strip(), value.strip()
                if key == "evidence_paths":
                    # The tool contract wants a LIST; a raw string would be
                    # char-split by an unaware consumer. Comma-separated form.
                    args[key] = [p for p in (s.strip() for s in value.split(","))
                                 if p]
                else:
                    args[key] = value
        handler = make_tool_handler(ctx, incidents_service)
        envelope = json.loads(handler(args))
        if not envelope.get("success"):
            return f"stabilize failed: {envelope.get('error')}"
        lines = [f"outcome: {envelope['outcome']}"]
        lines += [f"- {note}" for note in envelope.get("notes", [])]
        for stage, result in envelope.get("stage_results", {}).items():
            if isinstance(result, dict) and "skipped" in result:
                continue
            lines.append(f"{stage}: {json.dumps(result, ensure_ascii=False)[:400]}")
        return "\n".join(lines)

    return command


FIND_DUPLICATES_SCHEMA = {
    "name": "find_duplicate_incidents",
    "description": (
        "Search the local Jira incident index for likely duplicates of a bug "
        "summary (FTS + token-overlap ranking; PII redacted). Purely local — "
        "no tracker round-trip. Use before filing a new incident."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Bug title/summary to match."},
            "limit": {"type": "integer", "description": "Max candidates (default 5).",
                      "default": 5},
        },
        "required": ["summary"],
    },
}


def make_find_duplicates_handler(incidents_service):
    def handler(args, **kwargs) -> str:
        from . import dedup as dedup_mod

        try:
            args = args if isinstance(args, dict) else {}
            summary = args.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                return json.dumps({
                    "success": False,
                    "error": "Missing required argument 'summary'.",
                    "remediation": "Pass the bug title/summary text to match.",
                })
            incidents_service.ensure_initialized()
            result = dedup_mod.find_duplicate_incidents(
                incidents_service.store, summary, args.get("limit", 5)
            )
            return json.dumps({"success": True, **result}, ensure_ascii=False)
        except Exception as exc:
            logger.exception("find_duplicate_incidents crashed")
            return json.dumps({
                "success": False,
                "error": f"internal error ({type(exc).__name__})",
                "remediation": "See the agent log for details.",
            })
    return handler
