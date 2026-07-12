"""Orchestrator scenario tests (plan Phase 6 acceptance) — all offline fakes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from hermes_flaky_stabilization.orchestrator import pipeline as pl
from hermes_flaky_stabilization.pii.gate import GateResult

REPORT = {
    "title": "Checkout broken on Safari",
    "summary": "Clicking checkout does nothing; contact ops@corp.com.",
    "reproduction_steps": ["Open cart", "Click checkout"],
    "expected_behavior": "Order review opens.",
    "actual_behavior": "Nothing happens.",
    "severity": "high",
    "severity_rationale": "Blocks purchases.",
    "missing_evidence": [],
}


def _triage_result(category: str) -> str:
    return json.dumps({
        "success": True, "category": category, "confidence": 0.9,
        "summary": f"{category} failure", "evidence": ["E boom"],
        "suggested_action": "act", "classification_method": "llm",
        "prior_seen": False, "prior_occurrences": 0, "project": "p",
        "signature": "s", "log_stats": {"original_bytes": 1, "hit_count": 1,
                                        "truncated": False, "low_signal": False},
    })


def _real_heal_report(test_id: str, repo_dir: str, *, stable: bool, runs: int) -> dict:
    """The healer's REAL report envelope, pinned to the actual dataclasses.

    ``report["burnin"]`` is the sandbox aggregate's ``RunReport.to_dict()``
    (``runs``/``passed``/``failed``/…) inside ``HealReport.to_dict()`` — NOT
    the ``burn_in``/``repeats`` shape an earlier fake invented. Constructed
    through the real classes when importable so contract drift fails loudly;
    the fallback copies the exact real keys.
    """
    passed = runs if stable else max(runs - 1, 0)
    failed = 0 if stable else min(runs, 1)
    try:
        from hermes_flaky_stabilization.healer.flaky_healer.healer import HealReport
        from hermes_flaky_stabilization.healer.flaky_healer.sandbox.base import RunReport

        burnin = RunReport(runs=runs, passed=passed, failed=failed,
                           isolation="subprocess", duration_s=1.2,
                           results=[]).to_dict()
        return HealReport(test_id=test_id, repo_dir=repo_dir, burnin=burnin,
                          stable=stable).to_dict()
    except ImportError:  # pragma: no cover — pin the real keys if imports move
        return {
            "test_id": test_id, "repo_dir": repo_dir, "diagnosis": None,
            "diagnosis_stage": None, "strategy": None, "ops": [],
            "reproduced": None, "repro": None,
            "burnin": {"runs": runs, "passed": passed, "failed": failed,
                       "isolation": "subprocess", "duration_s": 1.2,
                       "exit_codes": [], "timed_out_runs": 0,
                       "last_stdout_tail": ""},
            "stable": stable, "diff": "", "isolation": "subprocess",
            "llm_calls": 0, "recipe_used": False, "recipe_match": None,
            "recipe_signature": None, "recipe_fallback": False,
            "confidence_note": "", "error": None, "duration_s": 1.2,
        }


def _stages(calls, *, category="flaky", heal_stable=True, heal_pr=False,
            heal_runs=3, gate_ok=True, tracker_created=True, report=None):
    report_payload = REPORT if report is None else report

    def triage(call, incident_context=None):
        calls.append(("triage", call, incident_context))
        return _triage_result(category)

    def heal(call):
        calls.append(("heal", call))
        payload = {"mode": call.get("mode"),
                   "report": _real_heal_report(call["test_id"], call["repo_dir"],
                                               stable=heal_stable, runs=heal_runs)}
        if heal_pr:
            payload["pr"] = {"url": "https://github.com/o/r/pull/1"}
        return json.dumps(payload)

    def bugreport(call):
        calls.append(("bugreport", call))
        return json.dumps(report_payload)

    def dedup(summary):
        calls.append(("dedup", summary))
        return {"candidates": [{"key": "INC-1", "summary": "similar",
                                "status": "Open", "score": 0.4}], "count": 1}

    def gate(paths):
        calls.append(("gate", list(paths)))
        if gate_ok:
            return GateResult(ok=True, checked_paths=[str(p) for p in paths])
        return GateResult(ok=False, checked_paths=[str(p) for p in paths],
                          findings_summary={"email": 2}, dirty_paths=[str(paths[0])])

    def tracker(call):
        calls.append(("tracker", call))
        if tracker_created:
            return json.dumps({"success": True, "created": True, "key": "INC-9",
                               "url": "https://x/browse/INC-9"})
        return json.dumps({"success": False, "error": "tracker_write_disabled"})

    def history_lookup(test_id):
        calls.append(("history", test_id))
        return {"success": True, "test_id": test_id, "failure_count": 3,
                "total_runs": 5, "last_failure_at": "2026-07-01T00:00:00"}

    def is_flaky(test_id):
        calls.append(("is_flaky", test_id))
        return {"success": True, "test_id": test_id, "is_flaky": category == "flaky",
                "status": "flaky" if category == "flaky" else "stable"}

    def ingest_synthetic(**kwargs):
        calls.append(("ingest_synthetic", kwargs))
        return 42

    def incident_search(query):
        calls.append(("incident_search", query))
        return [{"key": "INC-5", "summary": "related", "status": "Open"}]

    return pl.Stages(
        fetch_ci_logs=None, history_lookup=history_lookup, is_flaky=is_flaky,
        triage=triage, heal=heal, bugreport=bugreport, dedup=dedup, gate=gate,
        tracker=tracker, ingest_synthetic=ingest_synthetic,
        incident_search=incident_search,
    )


def _pipeline(calls, *, config=None, **kw):
    ledger = []
    p = pl.Pipeline(stages=_stages(calls, **kw),
                    config=config or {"pipeline": {"default_heal_mode": "suggest",
                                                   "heal_categories": ["flaky", "timeout"],
                                                   "require_pii_gate": True},
                                      "jira": {"enable_write": True}},
                    record_run=ledger.append)
    return p, ledger


def _log(tmp_path, text="E AssertionError: boom\nFAILED shop::kw_checkout\n"):
    path = tmp_path / "fail.log"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _assert_all_stage_keys(envelope):
    assert set(envelope["stage_results"]) == set(pl.STAGE_KEYS)


# --- fork behavior -------------------------------------------------------------


def test_flaky_fork_calls_healer_not_bugreport(tmp_path):
    calls = []
    p, ledger = _pipeline(calls, category="flaky")
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "shop::kw_checkout",
                 "repo_dir": str(tmp_path)})
    assert out["outcome"] == "healed"
    stage_names = [c[0] for c in calls]
    assert "heal" in stage_names and "bugreport" not in stage_names
    assert out["stage_results"]["bugreport"] == {
        "skipped": "heal branch taken (category 'flaky')"}
    _assert_all_stage_keys(out)
    assert ledger and ledger[0]["branch"] == "heal"


def test_bug_fork_calls_bugreport_not_healer(tmp_path):
    calls = []
    p, ledger = _pipeline(calls, category="broken_test")
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "shop::kw_checkout"})
    assert out["outcome"] == "ticket_created"
    stage_names = [c[0] for c in calls]
    assert "bugreport" in stage_names and "heal" not in stage_names
    assert "skipped" in out["stage_results"]["healer"]
    _assert_all_stage_keys(out)
    assert ledger[0]["branch"] == "bug"


def test_timeout_category_goes_to_healer(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="timeout")
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "t", "repo_dir": "."})
    assert out["outcome"] == "healed"
    assert "heal" in [c[0] for c in calls]


def test_incident_context_seeds_triage(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="flaky")
    p.run({"log_url_or_path": _log(tmp_path), "test_id": "t", "repo_dir": "."})
    triage_call = next(c for c in calls if c[0] == "triage")
    assert triage_call[2] == [{"key": "INC-5", "summary": "related", "status": "Open"}]


# --- heal outcomes + loop 1 --------------------------------------------------------


def test_stable_heal_feeds_burnin_back_into_history(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="flaky", heal_stable=True)
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "shop.cart::kw_checkout",
                 "repo_dir": "."})
    synths = [c for c in calls if c[0] == "ingest_synthetic"]
    assert len(synths) == 3
    cases = synths[0][1]["cases"]
    assert len(cases) == 1 and cases[0]["status"] == "passed"
    assert cases[0]["classname"] == "shop.cart" and cases[0]["name"] == "kw_checkout"
    assert any("fed back into test history" in n for n in out["notes"])


def test_pr_mode_stable_heal_reports_pr_opened(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="flaky", heal_stable=True, heal_pr=True)
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "t", "repo_dir": ".",
                 "mode": "pr"})
    assert out["outcome"] == "pr_opened"
    heal_call = next(c for c in calls if c[0] == "heal")
    assert heal_call[1]["mode"] == "pr"


def test_unstable_heal_needs_attention(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="flaky", heal_stable=False)
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "t", "repo_dir": "."})
    assert out["outcome"] == "needs_attention"
    assert not any(c[0] == "ingest_synthetic" for c in calls)


def test_flaky_without_repo_dir_needs_attention(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="flaky")
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "t"})
    assert out["outcome"] == "needs_attention"
    assert "skipped" in out["stage_results"]["healer"]
    _assert_all_stage_keys(out)


# --- bug branch: gate + tracker ------------------------------------------------------


def test_dirty_evidence_stops_pipeline_with_no_tracker_call(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="broken_test", gate_ok=False)
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["outcome"] == "needs_attention"
    assert out["stage_results"]["pii"]["ok"] is False
    assert out["stage_results"]["pii"]["findings_summary"] == {"email": 2}
    assert "skipped" in out["stage_results"]["tracker"]
    assert not any(c[0] == "tracker" for c in calls)  # nothing was sent
    _assert_all_stage_keys(out)


def test_gate_disabled_by_config_is_audited(tmp_path):
    """SECURITY (audit): require_pii_gate=false must not send silently. The run
    surfaces the disabled-gate decision in notes and in the pii stage result, so
    an operator opt-out is observable rather than a quiet bypass."""
    calls = []
    p, _ = _pipeline(calls, category="broken_test",
                     config={"pipeline": {"require_pii_gate": False,
                                          "heal_categories": ["flaky", "timeout"],
                                          "default_heal_mode": "suggest"},
                             "jira": {"enable_write": False}})
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["stage_results"]["pii"]["require_pii_gate"] is False
    assert any("DISABLED" in n for n in out["notes"])


def test_enable_write_false_returns_redacted_ticket_body(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="broken_test",
                     config={"pipeline": {"require_pii_gate": True,
                                          "heal_categories": ["flaky", "timeout"],
                                          "default_heal_mode": "suggest"},
                             "jira": {"enable_write": False}})
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["outcome"] == "ticket_ready"
    ticket = out["stage_results"]["tracker"]["ticket"]
    assert ticket["title"] == REPORT["title"]
    # The OUTBOUND ticket body is redacted (D6.4b). The bugreport stage result
    # keeps improve_bug_report's own unredacted contract — it never leaves the
    # machine; only the ticket does.
    assert "[redacted-email]" in ticket["body"]
    assert "ops@corp.com" not in json.dumps(ticket)
    assert not any(c[0] == "tracker" for c in calls)


def test_enable_write_true_creates_ticket(tmp_path):
    calls = []
    p, ledger = _pipeline(calls, category="broken_test", tracker_created=True)
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["outcome"] == "ticket_created"
    tracker_call = next(c for c in calls if c[0] == "tracker")
    assert tracker_call[1]["title"] == REPORT["title"]
    assert ledger[0]["outcome"] == "ticket_created"


def test_dedup_runs_on_bug_branch(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="broken_test")
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["stage_results"]["dedup"]["count"] == 1
    dedup_call = next(c for c in calls if c[0] == "dedup")
    assert REPORT["title"] in dedup_call[1]


# --- degenerate inputs ---------------------------------------------------------------


def test_no_inputs_needs_attention():
    calls = []
    p, _ = _pipeline(calls)
    out = p.run({})
    assert out["outcome"] == "needs_attention"
    _assert_all_stage_keys(out)
    assert all(isinstance(v, dict) for v in out["stage_results"].values())


def test_no_triage_falls_back_on_flaky_verdict(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="flaky")
    out = p.run({"test_id": "t", "repo_dir": "."})  # no log at all
    assert "skipped" in out["stage_results"]["triage"]
    assert out["outcome"] == "healed"  # detective verdict drove the fork
    assert any("detective verdict" in n for n in out["notes"])


# --- loop 1 contract: the reader must consume the healer's REAL envelope --------------


def test_burnin_feedback_reads_the_real_heal_envelope(tmp_path):
    """Regression: _feed_back_burnin read report['burn_in']['repeats'] — keys
    the healer never emits (real shape: report['burnin'] with runs/passed) —
    so every stable heal fed back exactly ONE synthetic row and the note
    reported a false count."""
    calls = []
    p, _ = _pipeline(calls, category="flaky", heal_stable=True, heal_runs=7)
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "t", "repo_dir": "."})
    assert out["outcome"] == "healed"
    synths = [c for c in calls if c[0] == "ingest_synthetic"]
    assert len(synths) == 7
    assert all(c[1]["cases"][0]["status"] == "passed" for c in synths)
    assert any("7 synthetic green run(s)" in n for n in out["notes"])


# --- outbound ticket: title redaction + the egress canary (D7/D6.4b) ------------------


def _write_disabled_config():
    return {"pipeline": {"require_pii_gate": True,
                         "heal_categories": ["flaky", "timeout"],
                         "default_heal_mode": "suggest"},
            "jira": {"enable_write": False}}


def test_ticket_ready_title_is_redacted(tmp_path):
    """Regression: the ticket_ready path redacted only the BODY; the title was
    returned raw (and stored raw in the ledger) for hand-pasting into Jira."""
    calls = []
    dirty = dict(REPORT, title="Checkout broken on Safari, seen by ops@corp.com")
    p, _ = _pipeline(calls, category="broken_test", report=dirty,
                     config=_write_disabled_config())
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["outcome"] == "ticket_ready"
    ticket = out["stage_results"]["tracker"]["ticket"]
    assert "[redacted-email]" in ticket["title"]
    # The tracker stage result (ledger-bound and outbound) never carries the raw
    # address; the bugreport stage result deliberately keeps its own contract.
    assert "ops@corp.com" not in json.dumps(out["stage_results"]["tracker"])


def test_egress_canary_masks_scanner_only_pii_in_ready_ticket(tmp_path):
    """Regression: a valid IBAN and a space-separated SSN pass redact_text but
    are found by the plugin's own detectors — the egress canary must mask them
    before the ticket leaves, and record that it fired."""
    calls = []
    dirty = dict(REPORT, summary=("Payment failed for IBAN GB82WEST12345698765432 "
                                  "and SSN 123 45 6789."))
    p, _ = _pipeline(calls, category="broken_test", report=dirty,
                     config=_write_disabled_config())
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["outcome"] == "ticket_ready"
    ticket = out["stage_results"]["tracker"]["ticket"]
    assert "GB82WEST12345698765432" not in ticket["body"]
    assert "123 45 6789" not in ticket["body"]
    assert ticket["egress_masked"] is True
    assert any("egress canary" in n for n in out["notes"])


def test_egress_canary_applies_to_the_enabled_tracker_payload(tmp_path):
    """The same canary must clean the payload handed to the tracker stage when
    jira.enable_write is on."""
    calls = []
    dirty = dict(REPORT,
                 title="Refund fails for IBAN GB82WEST12345698765432",
                 summary="Customer SSN 123 45 6789 shown in the error page.")
    p, _ = _pipeline(calls, category="broken_test", report=dirty)
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["outcome"] == "ticket_created"
    tracker_call = next(c for c in calls if c[0] == "tracker")
    sent = json.dumps(tracker_call[1])
    assert "GB82WEST12345698765432" not in sent
    assert "123 45 6789" not in sent
    assert any("egress canary" in n for n in out["notes"])


# --- evidence_paths validation (untrusted arg shapes) ----------------------------------


def test_lone_string_evidence_path_is_one_path_not_chars(tmp_path):
    """Regression: /stabilize stores every key=value as a STRING; list(str)
    char-split it into single-character 'paths' and the gate walked '/'."""
    calls = []
    p, _ = _pipeline(calls, category="broken_test")
    extra = str(tmp_path / "extra.log")
    Path(extra).write_text("clean\n", encoding="utf-8")
    log = _log(tmp_path)
    out = p.run({"log_url_or_path": log, "evidence_paths": extra})
    gate_call = next(c for c in calls if c[0] == "gate")
    assert gate_call[1] == [extra, log]          # one path, not len(extra) chars
    assert out["outcome"] == "ticket_created"


def test_non_list_evidence_paths_is_a_clean_stage_error(tmp_path):
    """Regression: a non-iterable evidence_paths raised TypeError out of run(),
    losing every stage result and the ledger row."""
    calls = []
    p, ledger = _pipeline(calls, category="broken_test")
    out = p.run({"log_url_or_path": _log(tmp_path), "evidence_paths": 7})
    assert out["outcome"] == "needs_attention"
    assert "error" in out["stage_results"]["pii"]
    assert "skipped" in out["stage_results"]["tracker"]
    assert not any(c[0] in ("gate", "tracker") for c in calls)
    assert ledger and ledger[0]["branch"] == "bug"   # row still written
    _assert_all_stage_keys(out)


# --- required PII gate fails closed when the stage is missing --------------------------


def test_missing_gate_stage_fails_closed_when_required(tmp_path):
    """Regression: stages.gate=None with require_pii_gate=true proceeded to the
    tracker with results['pii'] = {'skipped': ...} — fail OPEN."""
    calls = []
    stages = _stages(calls, category="broken_test")
    stages.gate = None
    ledger = []
    p = pl.Pipeline(stages=stages,
                    config={"pipeline": {"require_pii_gate": True},
                            "jira": {"enable_write": True}},
                    record_run=ledger.append)
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["outcome"] == "needs_attention"
    assert out["stage_results"]["pii"]["ok"] is False
    assert "skipped" in out["stage_results"]["tracker"]
    assert not any(c[0] == "tracker" for c in calls)  # nothing was sent
    assert ledger and ledger[0]["branch"] == "bug"


def test_gate_disabled_by_config_still_proceeds(tmp_path):
    calls = []
    p, _ = _pipeline(calls, category="broken_test",
                     config={"pipeline": {"require_pii_gate": False},
                             "jira": {"enable_write": True}})
    out = p.run({"log_url_or_path": _log(tmp_path)})
    assert out["outcome"] == "ticket_created"
    assert "skipped" in out["stage_results"]["pii"]
    assert not any(c[0] == "gate" for c in calls)


# --- evidence spooling: hostile build_id + spool failures -------------------------------


def _fetch_stages(calls, **kw):
    stages = _stages(calls, **kw)
    stages.fetch_ci_logs = lambda call: json.dumps(
        {"filtered_log": "E AssertionError: boom\n", "bytes_filtered": 22})
    return stages


def test_build_id_with_path_separator_is_sanitized(tmp_path):
    """Regression: build_id='123/456' was interpolated raw into the spool
    filename and raised FileNotFoundError out of run()."""
    calls = []
    ledger = []
    p = pl.Pipeline(stages=_fetch_stages(calls, category="flaky"),
                    config={"jira": {"enable_write": False}},
                    record_run=ledger.append)
    out = p.run({"build_id": "123/456", "repo": "o/r", "test_id": "t"})
    evidence = out["stage_results"]["evidence"]
    assert evidence["source"] == "fetch_ci_logs"
    spooled = Path(evidence["log"])
    assert spooled.name.startswith("ci-log-123-456-")
    assert spooled.suffix == ".log"
    assert spooled.parent == Path(".")
    assert ledger  # the run completed and was recorded


def test_spool_failure_is_an_evidence_stage_error_not_a_run_abort(monkeypatch):
    calls = []
    ledger = []
    p = pl.Pipeline(stages=_fetch_stages(calls, category="flaky"),
                    config={"jira": {"enable_write": False}},
                    record_run=ledger.append)

    def boom(self, text, build_id):
        raise OSError("read-only spool")

    monkeypatch.setattr(pl.Pipeline, "_spool_log", boom)
    out = p.run({"build_id": "b1", "repo": "o/r", "test_id": "t"})
    assert out["stage_results"]["evidence"] == {"error": "evidence spool failed (OSError)"}
    assert out["outcome"] == "needs_attention"
    assert ledger  # ledger row still written
    _assert_all_stage_keys(out)


# --- ledger: the branch is the fork decision, not a stage-shape guess -------------------


def test_heal_branch_without_repo_dir_still_records_heal_branch(tmp_path):
    """Regression: a heal-branch run that skipped the healer (no repo_dir)
    recorded branch=NULL — undercounting exactly the failures one queries for."""
    calls = []
    p, ledger = _pipeline(calls, category="flaky")
    out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "t"})  # no repo_dir
    assert out["outcome"] == "needs_attention"
    assert "skipped" in out["stage_results"]["healer"]
    assert ledger and ledger[0]["branch"] == "heal"


# --- incidents→triage feedback failures must be visible ---------------------------------


def test_incident_lookup_failure_is_logged_and_noted(tmp_path, caplog):
    """Regression: incident_search errors were swallowed bare — invisible, and
    indistinguishable from 'no similar incidents'."""
    calls = []
    p, _ = _pipeline(calls, category="broken_test")

    def boom(query):
        raise RuntimeError("fts index corrupt")

    p.stages.incident_search = boom
    with caplog.at_level(logging.WARNING,
                         logger="hermes_flaky_stabilization.orchestrator.pipeline"):
        out = p.run({"log_url_or_path": _log(tmp_path), "test_id": "t"})
    assert any("incident-context lookup failed (RuntimeError)" in n
               for n in out["notes"])
    assert any("incident-context lookup failed" in r.message for r in caplog.records)
    triage_call = next(c for c in calls if c[0] == "triage")
    assert triage_call[2] is None                 # triage still ran, without hints
    assert out["outcome"] == "ticket_created"     # ...and the run completed


# --- /stabilize argument parsing ---------------------------------------------------------


def _command_capture(monkeypatch):
    captured: dict = {}

    def fake_make_tool_handler(ctx, incidents_service):
        def handler(args):
            captured.update(args)
            return json.dumps({"success": True, "outcome": "needs_attention",
                               "stage_results": {}, "notes": []})
        return handler

    monkeypatch.setattr(pl, "make_tool_handler", fake_make_tool_handler)
    return pl.make_command(None, None), captured


def test_command_routes_path_double_colon_head_to_test_id(monkeypatch):
    """Regression: the documented file_path::name form (with a path separator)
    was misrouted as a log path because os.path.sep was checked first."""
    command, captured = _command_capture(monkeypatch)
    command("tests/login.spec.ts::checkout repo_dir=/tmp/x")
    assert captured["test_id"] == "tests/login.spec.ts::checkout"
    assert "log_url_or_path" not in captured


def test_command_existing_file_with_double_colon_stays_log_path(tmp_path, monkeypatch):
    command, captured = _command_capture(monkeypatch)
    weird = tmp_path / "a::b.log"
    weird.write_text("E boom\n", encoding="utf-8")
    command(str(weird))
    assert captured["log_url_or_path"] == str(weird)
    assert "test_id" not in captured


def test_command_splits_evidence_paths_into_a_list(monkeypatch):
    """Regression: /stabilize stored evidence_paths as one string, which the
    pipeline would previously char-split into nonsense."""
    command, captured = _command_capture(monkeypatch)
    command("some_test evidence_paths=/tmp/a.log,/tmp/b.log")
    assert captured["evidence_paths"] == ["/tmp/a.log", "/tmp/b.log"]
    command("some_test evidence_paths=/tmp/only.log")
    assert captured["evidence_paths"] == ["/tmp/only.log"]


# --- the real ingest_synthetic + lookup round trip (loop 1 end-to-end) ----------------


def test_synthetic_run_visible_via_test_failure_lookup(profile_env):
    from hermes_flaky_stabilization.history import (
        _handle_test_failure_lookup,
        storage,
    )
    from hermes_flaky_stabilization.history import ingest as history_ingest

    storage.reset_for_tests()
    conn = storage.get_connection()
    conn.execute(
        "INSERT INTO test_runs (suite_name, run_timestamp, total, failures, errors,"
        " skipped, source_file) VALUES ('s', '2026-07-01T00:00:00', 1, 1, 0, 0, 'f')")
    run_id = conn.execute("SELECT id FROM test_runs").fetchone()[0]
    conn.execute(
        "INSERT INTO test_cases (run_id, classname, name, file_path, status,"
        " failure_message) VALUES (?, 'shop.cart', 'kw_checkout', 'src/c.py',"
        " 'failed', 'boom')", (run_id,))
    conn.commit()

    before = json.loads(_handle_test_failure_lookup({"test_id": "kw_checkout"}))
    assert before["total_runs"] == 1

    history_ingest.ingest_synthetic_run(conn, cases=[
        {"classname": "shop.cart", "name": "kw_checkout",
         "file_path": "src/c.py", "status": "passed"}] * 5)

    after = json.loads(_handle_test_failure_lookup({"test_id": "kw_checkout"}))
    assert after["total_runs"] == 2              # two distinct test runs
    assert after["failure_count"] == before["failure_count"]
    storage.reset_for_tests()


def test_registered_tool_end_to_end_heuristic_path(profile_env, tmp_path):
    """The REAL wiring (build_pipeline): register via the unified entry, call
    the stabilize_test_failure handler on a local log with no LLM queued —
    triage degrades to the heuristic, the bug branch produces ticket_ready."""
    from _doubles import FakePluginContext

    import hermes_flaky_stabilization as plugin

    ctx = FakePluginContext(hermes_home=profile_env)
    plugin.register(ctx)

    log = tmp_path / "fail.log"
    log.write_text(
        "E   ModuleNotFoundError: No module named 'requests'\n"
        "FAILED tests/test_api.py::test_fetch\n", encoding="utf-8")
    handler = ctx.tools["stabilize_test_failure"]["handler"]
    out = json.loads(handler({"log_url_or_path": str(log), "project": "e2e"}))
    assert out["success"] is True
    assert set(out["stage_results"]) == set(pl.STAGE_KEYS)
    triage_result = out["stage_results"]["triage"]
    assert triage_result["classification_method"] == "heuristic"
    # environment category (ModuleNotFoundError) -> bug branch; write disabled
    # by default -> the redacted ticket body comes back to file manually.
    assert out["outcome"] in ("ticket_ready", "needs_attention")
    if out["outcome"] == "ticket_ready":
        assert out["stage_results"]["tracker"]["ticket"]["title"]

    # The run landed in the pipeline_runs ledger.
    from contextlib import closing

    from hermes_flaky_stabilization.storage import state

    with closing(state.connect()) as conn:
        rows = conn.execute("SELECT outcome, branch FROM pipeline_runs").fetchall()
    assert len(rows) == 1
