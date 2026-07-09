"""Orchestrator scenario tests (plan Phase 6 acceptance) — all offline fakes."""

from __future__ import annotations

import json

from hermes_flaky_stabilization.orchestrator import pipeline as pl
from hermes_flaky_stabilization.orchestrator.gate import GateResult

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


def _stages(calls, *, category="flaky", heal_stable=True, heal_pr=False,
            gate_ok=True, tracker_created=True):
    def triage(call, incident_context=None):
        calls.append(("triage", call, incident_context))
        return _triage_result(category)

    def heal(call):
        calls.append(("heal", call))
        payload = {"mode": call.get("mode"),
                   "report": {"stable": heal_stable, "test_id": call["test_id"],
                              "burn_in": {"repeats": 3, "failures": 0}}}
        if heal_pr:
            payload["pr"] = {"url": "https://github.com/o/r/pull/1"}
        return json.dumps(payload)

    def bugreport(call):
        calls.append(("bugreport", call))
        return json.dumps(REPORT)

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
    synth = next(c for c in calls if c[0] == "ingest_synthetic")
    cases = synth[1]["cases"]
    assert len(cases) == 3 and all(c["status"] == "passed" for c in cases)
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
    assert after["total_runs"] == 6              # 1 failed + 5 synthetic green
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
