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

# The stages only the bug branch runs. Named once: the heal branch marks exactly
# these as skipped, and so does the "nothing to act on" fall-through.
_BUG_BRANCH_STAGES = ("bugreport", "dedup", "pii", "tracker")

OUTCOME_HEALED = "healed"
OUTCOME_PR_OPENED = "pr_opened"
OUTCOME_TICKET_CREATED = "ticket_created"
OUTCOME_TICKET_READY = "ticket_ready"
OUTCOME_NEEDS_ATTENTION = "needs_attention"

_INCIDENT_HINT_LIMIT = 3


def _as_dict(raw: Any) -> Any:
    """Normalise a stage's return value.

    Stage callables honour the tool contract (a JSON string) in production but
    the injected test doubles return plain dicts; every call site used to
    re-inline this ternary.
    """
    return json.loads(raw) if isinstance(raw, str) else raw


def _stage_error(label: str, exc: Exception) -> dict[str, str]:
    """The uniform stage-error envelope.

    The exception *type* is reported, never its message: a stage failure must
    not leak a token or a path out of a third-party client's error string.
    """
    return {"error": f"{label} failed ({type(exc).__name__})"}


def _internal_error_envelope(exc: Exception) -> str:
    """The tool-contract envelope for a crash inside a registered handler."""
    return json.dumps({
        "success": False,
        "error": f"internal error ({type(exc).__name__})",
        "remediation": "See the agent log for details.",
    })


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


def _fresh_results() -> dict[str, Any]:
    return {key: {"skipped": "not reached"} for key in STAGE_KEYS}


@dataclass
class RunContext:
    """One run's state: the arguments in, the stage results and notes out.

    Every stage step used to take ``(args, results, notes)`` positionally and
    untyped, mutating two bare accumulators in place. One named object makes the
    signatures typed and gives the skip / note / error idioms a single spelling,
    so a stage cannot invent its own shape for "I was skipped".
    """

    args: dict[str, Any]
    results: dict[str, Any] = field(default_factory=_fresh_results)
    notes: list[str] = field(default_factory=list)

    def arg(self, name: str) -> str:
        """A trimmed string argument; ``""`` when absent or non-string."""
        return str(self.args.get(name) or "").strip()

    def note(self, message: str) -> None:
        """Leave an operator-visible note on the run."""
        self.notes.append(message)

    def skip(self, *keys: str, reason: str) -> None:
        """Mark one or more stages as not taken, with the reason why."""
        for key in keys:
            self.results[key] = {"skipped": reason}

    def fail(self, key: str, label: str, exc: Exception) -> None:
        """Record a stage error. Reports the exception TYPE, never its message:
        a third-party client's error string can carry a token or a path."""
        self.results[key] = _stage_error(label, exc)


@dataclass
class Pipeline:
    """The orchestrator; construct via :func:`build_pipeline` for real wiring."""

    stages: Stages
    config: dict[str, Any] = field(default_factory=dict)
    record_run: Callable[[dict], None] | None = None

    @property
    def _pipeline_cfg(self) -> dict[str, Any]:
        """The ``pipeline`` config section, always a dict."""
        return self.config.get("pipeline") or {}

    # -- public entry -----------------------------------------------------

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        ctx = RunContext(args=args if isinstance(args, dict) else {})

        heal_categories = self._pipeline_cfg.get("heal_categories") or ["flaky", "timeout"]

        log_path = self._evidence(ctx)
        test_id = ctx.arg("test_id")
        self._history(test_id, ctx)
        category = self._triage(ctx, log_path, test_id)

        outcome = OUTCOME_NEEDS_ATTENTION
        if category is None and self._verdict_is_flaky(ctx.results["detective"]):
            category = "flaky"
            ctx.note("no triage category; forked on the detective verdict")

        # The branch is decided HERE, at the fork, and recorded as such — the
        # ledger must not reverse-engineer it from stage-result shapes (a heal
        # branch that skipped the healer would otherwise record branch=NULL).
        branch: str | None = None
        if category in heal_categories:
            branch = "heal"
            ctx.skip(*_BUG_BRANCH_STAGES, reason=f"heal branch taken (category {category!r})")
            outcome = self._heal_branch(ctx, test_id)
        elif category is not None:
            branch = "bug"
            ctx.skip("healer", reason=f"bug branch taken (category {category!r})")
            outcome = self._bug_branch(ctx, log_path, test_id)
        else:
            ctx.skip("healer", *_BUG_BRANCH_STAGES,
                     reason="no category and no flaky verdict")
            ctx.note("nothing to act on: supply a log and/or a known test_id")

        envelope = {
            "success": True,
            "outcome": outcome,
            "stage_results": ctx.results,
            "notes": ctx.notes,
        }
        self._ledger(ctx, category, branch, outcome, time.monotonic() - started)
        return envelope

    # -- stage steps --------------------------------------------------------

    def _evidence(self, ctx: RunContext) -> str | None:
        log = ctx.arg("log_url_or_path")
        if log:
            ctx.results["evidence"] = {"source": "given_log", "log": log}
            return log
        build_id = ctx.arg("build_id")
        repo = ctx.arg("repo")
        if build_id and repo and self.stages.fetch_ci_logs is not None:
            try:
                data = _as_dict(self.stages.fetch_ci_logs({"build_id": build_id, "repo": repo}))
            except Exception as exc:
                ctx.fail("evidence", "fetch_ci_logs", exc)
                return None
            if isinstance(data, dict) and data.get("filtered_log"):
                try:
                    # Inside the stage's error envelope: an unwritable spool dir
                    # (or any other spool failure) is an evidence-stage error,
                    # not a run abort that loses every result and the ledger row.
                    path = self._spool_log(data["filtered_log"], build_id)
                except Exception as exc:
                    ctx.fail("evidence", "evidence spool", exc)
                    return None
                ctx.results["evidence"] = {
                    "source": "fetch_ci_logs", "log": str(path),
                    "bytes_filtered": data.get("bytes_filtered"),
                }
                return str(path)
            ctx.results["evidence"] = {
                "error": (data or {}).get("error", "fetch_ci_logs returned no log")
                if isinstance(data, dict) else "fetch_ci_logs returned no log",
            }
            return None
        ctx.skip("evidence", reason="no log given (and no build_id+repo+token)")
        return None

    def _spool_log(self, text: str, build_id: str) -> Path:
        from .. import paths

        # Owner-only: the spooled CI log can carry secrets and captured output,
        # and get_data_dir()'s 0700 parent does not set this child's mode.
        spool = paths.get_data_dir() / "pipeline-evidence"
        spool.mkdir(parents=True, mode=0o700, exist_ok=True)
        # build_id is untrusted ("123/456" would escape the spool dir or just
        # fail on a missing subdirectory); sanitise before it names a file.
        safe_id = _SAFE_BUILD_ID_RE.sub("-", build_id) or "unknown"
        path = spool / f"ci-log-{safe_id}.log"
        path.write_text(text, encoding="utf-8")
        paths.restrict_file(path)
        return path

    def _history(self, test_id: str, ctx: RunContext) -> None:
        if not test_id:
            ctx.skip("history", "detective", reason="no test_id given")
            return
        if self.stages.history_lookup is not None:
            try:
                ctx.results["history"] = self.stages.history_lookup(test_id)
            except Exception as exc:
                ctx.fail("history", "history lookup", exc)
        else:
            ctx.skip("history", reason="history stage unavailable")
        if self.stages.is_flaky is not None:
            try:
                ctx.results["detective"] = self.stages.is_flaky(test_id)
            except Exception as exc:
                ctx.fail("detective", "is_flaky", exc)
        else:
            ctx.skip("detective", reason="detective stage unavailable")

    @staticmethod
    def _verdict_is_flaky(detective_result) -> bool:
        return isinstance(detective_result, dict) and detective_result.get("is_flaky") is True

    def _incident_context(self, log_path: str | None, test_id: str,
                          ctx: RunContext) -> list | None:
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
                from .. import triage

                query = triage.signal_query(raw) or query
            except Exception as exc:
                logger.warning("incident-context query derivation failed", exc_info=True)
                ctx.note(
                    f"incident-context query derivation failed "
                    f"({type(exc).__name__}) — fell back to the bare test id"
                )
        if not query:
            return None
        try:
            rows = self.stages.incident_search(query)
        except Exception as exc:
            logger.warning("incident-context lookup failed", exc_info=True)
            ctx.note(
                f"incident-context lookup failed ({type(exc).__name__}) — "
                f"triage ran without incident hints"
            )
            return None
        return rows or None

    def _triage(self, ctx: RunContext, log_path, test_id) -> str | None:
        if not log_path:
            ctx.skip("triage", reason="no log evidence to triage")
            return None
        if self.stages.triage is None:
            ctx.skip("triage", reason="triage stage unavailable")
            return None
        incident_context = self._incident_context(log_path, test_id, ctx)
        call: dict[str, Any] = {"log_url_or_path": log_path}
        if ctx.args.get("project"):
            call["project"] = ctx.args["project"]
        try:
            data = _as_dict(self.stages.triage(call, incident_context=incident_context))
        except Exception as exc:
            ctx.fail("triage", "triage", exc)
            return None
        ctx.results["triage"] = data
        if not isinstance(data, dict) or not data.get("success"):
            return None
        return data.get("category")

    # -- heal branch ---------------------------------------------------------

    def _heal_branch(self, ctx: RunContext, test_id) -> str:
        repo_dir = ctx.arg("repo_dir")
        if not repo_dir or not test_id:
            ctx.skip("healer", reason="healer needs repo_dir and test_id")
            ctx.note("flaky category but no repo_dir/test_id — cannot heal")
            return OUTCOME_NEEDS_ATTENTION
        if self.stages.heal is None:
            ctx.skip("healer", reason="healer stage unavailable")
            return OUTCOME_NEEDS_ATTENTION
        mode = str(ctx.args.get("mode") or self._pipeline_cfg.get("default_heal_mode")
                   or "suggest")
        call = {"repo_dir": repo_dir, "test_id": test_id, "mode": mode}
        for optional in ("trace_path", "build_id", "repo"):
            if ctx.args.get(optional):
                call[optional] = ctx.args[optional]
        try:
            data = _as_dict(self.stages.heal(call))
        except Exception as exc:
            ctx.fail("healer", "heal", exc)
            return OUTCOME_NEEDS_ATTENTION
        ctx.results["healer"] = data
        if not isinstance(data, dict):
            return OUTCOME_NEEDS_ATTENTION
        report = data.get("report") or {}
        if report.get("stable"):
            self._feed_back_burnin(test_id, report, ctx)
            if data.get("pr"):
                return OUTCOME_PR_OPENED
            return OUTCOME_HEALED
        ctx.note("heal did not reach a stable burn-in")
        return OUTCOME_NEEDS_ATTENTION

    def _feed_back_burnin(self, test_id, report, ctx: RunContext) -> None:
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
            ctx.note(
                f"burn-in outcome fed back into test history (run_id={run_id}, "
                f"{len(cases)} green case row(s))"
            )
        except Exception as exc:
            ctx.note(f"burn-in feedback failed ({type(exc).__name__})")

    # -- bug branch ------------------------------------------------------------

    def _bug_branch(self, ctx: RunContext, log_path, test_id) -> str:
        report = self._bugreport(ctx, log_path, test_id)
        if report is None:
            return OUTCOME_NEEDS_ATTENTION

        self._dedup(ctx, report)

        evidence_paths = self._coerce_evidence_paths(ctx)
        if evidence_paths is None:
            return OUTCOME_NEEDS_ATTENTION
        if log_path and not str(log_path).startswith("http"):
            evidence_paths.append(str(log_path))

        if not self._pii_gate(ctx, evidence_paths):
            return OUTCOME_NEEDS_ATTENTION

        return self._tracker(ctx, report, evidence_paths)

    def _dedup(self, ctx: RunContext, report: dict) -> None:
        summary_query = f"{report.get('title', '')} {report.get('summary', '')}".strip()
        if self.stages.dedup is not None and summary_query:
            try:
                ctx.results["dedup"] = self.stages.dedup(summary_query)
            except Exception as exc:
                ctx.fail("dedup", "dedup", exc)
        else:
            ctx.skip("dedup", reason="no dedup stage or empty summary")

    def _pii_gate(self, ctx: RunContext, evidence_paths: list[str]) -> bool:
        """Run (or knowingly skip) the PII gate. ``False`` blocks the tracker."""
        if not self._pipeline_cfg.get("require_pii_gate", True):
            # An explicit operator opt-out (require_pii_gate=false, default true)
            # sends the ticket with no PII gate — exactly the decision that should
            # be auditable, so surface it in notes and log a warning like the
            # fail paths around it, rather than passing silently.
            ctx.results["pii"] = {"skipped": "gate disabled by config",
                                  "require_pii_gate": False}
            ctx.note(
                "PII gate DISABLED by config (require_pii_gate=false) — evidence "
                "sent without a PII scan"
            )
            logger.warning(
                "pipeline: PII gate disabled by config (require_pii_gate=false); "
                "tracker evidence bypassed the PII scan"
            )
            return True

        if self.stages.gate is None:
            # Fail CLOSED, mirroring assert_evidence_clean's scan-error
            # policy: a required gate that cannot run must block the
            # tracker, not wave the evidence through.
            ctx.results["pii"] = {
                "ok": False,
                "error": "pii gate required by config but unavailable (fail closed)",
            }
            ctx.skip("tracker", reason="pii_gate_unavailable — nothing sent")
            ctx.note("PII gate required but unavailable — failing closed")
            return False

        gate_result = self.stages.gate(evidence_paths)
        gate_dict = gate_result.to_dict() if hasattr(gate_result, "to_dict") else gate_result
        ctx.results["pii"] = gate_dict
        if not gate_dict.get("ok"):
            ctx.skip("tracker", reason="pii_gate_failed — nothing sent")
            ctx.note("PII gate failed: scrub or drop the flagged evidence")
            return False
        return True

    @staticmethod
    def _coerce_evidence_paths(ctx: RunContext) -> list[str] | None:
        """Validate the untrusted ``evidence_paths`` argument.

        The slash command (and models) routinely pass a lone string — iterating
        it would char-split into single-character "paths" and send the gate
        walking the filesystem root; a non-iterable would raise ``TypeError``
        out of ``run()`` and lose the ledger row. A lone string is one path;
        any other non-list shape is a clean stage error (``None`` return —
        the caller ends the run as ``needs_attention`` with the row written).
        """
        raw = ctx.args.get("evidence_paths")
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw] if raw.strip() else []
        if isinstance(raw, (list, tuple)):
            return [str(p) for p in raw]
        ctx.results["pii"] = {
            "ok": False,
            "error": (f"invalid evidence_paths type ({type(raw).__name__}); "
                      "expected a list of path strings"),
        }
        ctx.skip("tracker", reason="invalid evidence_paths — nothing sent")
        ctx.note("evidence_paths must be a list of file paths (or one path string)")
        return None

    def _bugreport(self, ctx: RunContext, log_path, test_id) -> dict | None:
        if self.stages.bugreport is None:
            ctx.skip("bugreport", reason="bugreport stage unavailable")
            return None
        raw_text = self._raw_evidence_text(log_path, test_id, ctx.results)
        try:
            data = _as_dict(self.stages.bugreport({"raw_text": raw_text, "format": "json"}))
        except Exception as exc:
            ctx.fail("bugreport", "bug report", exc)
            return None
        if not isinstance(data, dict) or data.get("error"):
            ctx.results["bugreport"] = data if isinstance(data, dict) else {"error": "bad report"}
            return None
        ctx.results["bugreport"] = data
        return data

    @staticmethod
    def _raw_evidence_text(log_path, test_id, results) -> str:
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

    def _tracker(self, ctx: RunContext, report, evidence_paths) -> str:
        from ..pii import redaction

        # D7: the TITLE is outbound text exactly like the body — redact it on
        # this side too (the enabled path's build_ticket re-redacts internally,
        # but the ticket_ready title is hand-pasted into Jira as returned).
        title, title_fired = self._egress_mask(
            redaction.redact_text(str(report.get("title") or "")))
        body, body_fired = self._egress_mask(self._ticket_body(report))
        egress_fired = title_fired or body_fired
        if egress_fired:
            ctx.note(
                "egress canary fired: PII remained after redaction and was "
                "masked in the outbound ticket text"
            )
        jira_cfg = self.config.get("jira") or {}
        if not jira_cfg.get("enable_write") or self.stages.tracker is None:
            ticket: dict[str, Any] = {"title": title, "body": body}
            if egress_fired:
                ticket["egress_masked"] = True
            ctx.results["tracker"] = {
                "skipped": "jira.enable_write is off",
                "ticket": ticket,
            }
            ctx.note("tracker write disabled — returning the ticket body to file manually")
            return OUTCOME_TICKET_READY
        try:
            data = _as_dict(self.stages.tracker({
                "title": title,
                "body": body,
                "severity": report.get("severity", ""),
                "evidence_paths": evidence_paths,
            }))
        except Exception as exc:
            ctx.fail("tracker", "tracker write", exc)
            return OUTCOME_NEEDS_ATTENTION
        ctx.results["tracker"] = data
        if isinstance(data, dict) and data.get("created"):
            return OUTCOME_TICKET_CREATED
        ctx.note("tracker write did not create an issue")
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

    def _ledger(self, ctx: RunContext, category, branch, outcome, duration_s) -> None:
        """Record the run; *branch* is the fork decision made in :meth:`run`
        (never reverse-engineered from stage-result shapes, which would record
        NULL for exactly the failed runs one queries the ledger for)."""
        if self.record_run is None:
            return
        try:
            self.record_run({
                "trigger": "tool",
                "test_id": ctx.args.get("test_id"),
                "project": ctx.args.get("project"),
                "triage_category": category,
                "branch": branch,
                "outcome": outcome,
                "stage_results_json": json.dumps(ctx.results, ensure_ascii=False, default=str),
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
    from .. import detective, history, triage
    from ..bugreport import handler as bugreport_handler
    from ..healer import handlers as healer_handlers
    from ..incidents import write as incidents_write
    from ..pii import gate as gate_mod

    cfg = unified_config.load_config()

    def triage_stage(call: dict, incident_context=None) -> str:
        return triage.run_triage(
            call, llm=getattr(ctx, "llm", None), incident_context=incident_context
        )

    def incident_search(query: str) -> list:
        # The redacted reader is the only incident surface the orchestrator
        # touches — it returns already-scrubbed key/summary/status projections.
        incidents_service.ensure_initialized()
        return incidents_service.reader.search(query, _INCIDENT_HINT_LIMIT)

    def dedup_stage(summary: str) -> dict:
        incidents_service.ensure_initialized()
        return incidents_service.reader.find_duplicates(summary)

    return Pipeline(
        stages=Stages(
            fetch_ci_logs=functools.partial(healer_handlers.fetch_ci_logs, ctx=ctx),
            history_lookup=history.failure_lookup,
            is_flaky=detective.flaky_verdict,
            triage=triage_stage,
            heal=functools.partial(healer_handlers.heal_flaky_test, ctx=ctx),
            bugreport=bugreport_handler.make_handler(ctx),
            dedup=dedup_stage,
            gate=gate_mod.assert_evidence_clean,
            tracker=incidents_write.handle_create_incident,
            ingest_synthetic=history.ingest_synthetic,
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
            return _internal_error_envelope(exc)
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
            result = incidents_service.reader.find_duplicates(summary, args.get("limit", 5))
            return json.dumps({"success": True, **result}, ensure_ascii=False)
        except Exception as exc:
            logger.exception("find_duplicate_incidents crashed")
            return _internal_error_envelope(exc)
    return handler
