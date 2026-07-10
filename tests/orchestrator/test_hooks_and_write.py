"""Approval escalation (pre_tool_call), the PII gate, dedup ranking, and the
net-new tracker write — plan Phase 6 acceptance."""

from __future__ import annotations

import json

from _doubles import FakePluginContext

from hermes_flaky_stabilization.incidents import dedup
from hermes_flaky_stabilization.orchestrator import hooks
from hermes_flaky_stabilization.pii import gate

# --- pre_tool_call escalation (D6.3) ------------------------------------------------


def _register(tmp_path):
    import hermes_flaky_stabilization as plugin

    ctx = FakePluginContext(hermes_home=tmp_path)
    plugin.register(ctx)
    return ctx


def test_heal_pr_mode_yields_approve_directive(tmp_path):
    ctx = _register(tmp_path)
    results = ctx.fire_hook("pre_tool_call", tool_name="heal_flaky_test",
                            args={"mode": "pr", "repo_dir": ".", "test_id": "t"})
    directives = [r for r in results if r]
    assert len(directives) == 1
    assert directives[0]["action"] == "approve"
    assert directives[0]["rule_key"] == "flaky_stab_pr"
    assert directives[0]["message"]


def test_heal_suggest_mode_yields_none(tmp_path):
    ctx = _register(tmp_path)
    results = ctx.fire_hook("pre_tool_call", tool_name="heal_flaky_test",
                            args={"mode": "suggest"})
    assert all(r is None for r in results)


def test_jira_create_incident_always_yields_directive(tmp_path):
    ctx = _register(tmp_path)
    results = ctx.fire_hook("pre_tool_call", tool_name="jira_create_incident",
                            args={"title": "x", "body": "y"})
    directives = [r for r in results if r]
    assert directives[0]["action"] == "approve"
    assert directives[0]["rule_key"] == "flaky_stab_tracker_write"


def test_other_tools_yield_none(tmp_path):
    ctx = _register(tmp_path)
    for name in ("test_failure_lookup", "is_flaky", "terminal", "jira_search_incident"):
        results = ctx.fire_hook("pre_tool_call", tool_name=name, args={})
        assert all(r is None for r in results), name


def test_pre_tool_call_is_pure_dict_inspection():
    # No registration/service needed: the hook body is a module function that
    # does no I/O for the per-tool rules — safe on the host's hot path. (The
    # stabilize rule below adds one local config read, never network I/O.)
    assert hooks.pre_tool_call(tool_name="heal_flaky_test", args={"mode": "pr"})
    assert hooks.pre_tool_call(tool_name="heal_flaky_test", args=None) is None
    assert hooks.pre_tool_call(tool_name="", args={}) is None


# --- stabilize_test_failure escalation (D6.3 closure) --------------------------------
#
# The pipeline calls its tracker/healer stages as PLAIN functions (no
# dispatch_tool), so the per-tool jira_create_incident/heal_flaky_test rules
# never fire inside a run — pre_tool_call must escalate the pipeline entry
# itself whenever the run could reach an external write.


def _write_unified_jira_cfg(home, **jira):
    from hermes_flaky_stabilization import config as unified_config

    unified_config.write_config({"jira": jira}, home / "flaky-stabilization")


def test_stabilize_pr_mode_yields_approve_directive(tmp_path):
    # Escalates even with tracker write dead (no token, write disabled): the
    # heal-PR path is itself a cross-host write.
    ctx = _register(tmp_path)
    results = ctx.fire_hook("pre_tool_call", tool_name="stabilize_test_failure",
                            args={"mode": "pr", "repo_dir": ".", "test_id": "t"})
    directives = [r for r in results if r]
    assert len(directives) == 1
    assert directives[0]["action"] == "approve"
    assert directives[0]["rule_key"] == "flaky_stab_stabilize_pipeline"
    assert directives[0]["message"]


def test_stabilize_escalates_when_jira_write_is_live(_isolated_env, monkeypatch):
    """SCENARIO: jira.enable_write=true + JIRA_API_TOKEN set — the pipeline's
    plain-function tracker stage could create a real Jira issue, so even a
    suggest-mode call must be approval-escalated."""
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    _write_unified_jira_cfg(_isolated_env, enable_write=True,
                            base_url="https://x.atlassian.net")
    directive = hooks.pre_tool_call(tool_name="stabilize_test_failure",
                                    args={"test_id": "t"})
    assert directive is not None
    assert directive["action"] == "approve"
    assert directive["rule_key"] == "flaky_stab_stabilize_pipeline"
    assert "Jira" in directive["message"]


def test_stabilize_stays_silent_when_no_write_is_reachable(_isolated_env, monkeypatch):
    # Token present but write disabled: the tracker handler refuses, no write.
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    _write_unified_jira_cfg(_isolated_env, enable_write=False)
    assert hooks.pre_tool_call(tool_name="stabilize_test_failure",
                               args={"mode": "suggest"}) is None
    # Write enabled but no token: the handler cannot authenticate, no write.
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    _write_unified_jira_cfg(_isolated_env, enable_write=True)
    assert hooks.pre_tool_call(tool_name="stabilize_test_failure",
                               args={}) is None


def test_stabilize_escalates_when_config_read_fails(monkeypatch):
    """Fail closed: an unreadable config with a token present must escalate."""
    from hermes_flaky_stabilization import config as unified_config

    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    def boom(*args, **kwargs):
        raise OSError("config unreadable")

    monkeypatch.setattr(unified_config, "load_config", boom)
    directive = hooks.pre_tool_call(tool_name="stabilize_test_failure", args={})
    assert directive is not None and directive["action"] == "approve"
    assert directive["rule_key"] == "flaky_stab_stabilize_pipeline"


def _write_unified_pipeline_cfg(home, **pipeline):
    from hermes_flaky_stabilization import config as unified_config

    unified_config.write_config({"pipeline": pipeline}, home / "flaky-stabilization")


def test_stabilize_pr_via_config_default_escalates(_isolated_env):
    """SECURITY (approval-gate bypass): a mode-less stabilize call resolves the
    heal mode from pipeline.default_heal_mode, exactly as the pipeline does. With
    the default set to 'pr' the run would open a PR, so the hook must escalate —
    previously it inspected args['mode'] only and stayed silent (no approval)."""
    _write_unified_pipeline_cfg(_isolated_env, default_heal_mode="pr")
    directive = hooks.pre_tool_call(
        tool_name="stabilize_test_failure",
        args={"repo_dir": ".", "test_id": "t"},  # NOTE: no explicit mode
    )
    assert directive is not None and directive["action"] == "approve"
    assert directive["rule_key"] == "flaky_stab_stabilize_pipeline"
    assert "pull request" in directive["message"]


def test_stabilize_suggest_config_default_stays_silent(_isolated_env):
    # The safe default (suggest) with no write reachable escalates nothing.
    _write_unified_pipeline_cfg(_isolated_env, default_heal_mode="suggest")
    assert hooks.pre_tool_call(
        tool_name="stabilize_test_failure",
        args={"repo_dir": ".", "test_id": "t"},
    ) is None


def test_explicit_suggest_overrides_pr_config_default(_isolated_env):
    # An explicit mode argument wins over the config default (both directions).
    _write_unified_pipeline_cfg(_isolated_env, default_heal_mode="pr")
    assert hooks.pre_tool_call(
        tool_name="stabilize_test_failure",
        args={"repo_dir": ".", "test_id": "t", "mode": "suggest"},
    ) is None


# --- PII gate (D6.4) -----------------------------------------------------------------


def test_gate_passes_clean_evidence(tmp_path):
    clean = tmp_path / "clean.log"
    clean.write_text("all good here\n", encoding="utf-8")
    result = gate.assert_evidence_clean([str(clean)])
    assert result.ok and not result.findings_summary


def test_gate_fails_closed_on_dirty_evidence(tmp_path):
    dirty = tmp_path / "dirty.log"
    dirty.write_text("card 4111 1111 1111 1111 and mail a@b.com\n", encoding="utf-8")
    result = gate.assert_evidence_clean([str(dirty)])
    assert not result.ok
    assert str(dirty) in result.dirty_paths
    assert result.findings_summary  # type:count aggregates only
    assert "4111" not in json.dumps(result.to_dict())  # never raw PII


def test_gate_fails_closed_on_missing_path(tmp_path):
    result = gate.assert_evidence_clean([str(tmp_path / "absent.log")])
    assert not result.ok
    assert result.errors or result.incomplete_paths or result.dirty_paths


def test_gate_with_no_paths_is_ok():
    result = gate.assert_evidence_clean([])
    assert result.ok and result.checked_paths == []


def test_gate_passes_configured_max_files_to_the_scanner(_isolated_env, monkeypatch,
                                                         tmp_path):
    """Regression: pii.default_max_files in the unified config was dead — the
    gate always called scan(path, None, None)."""
    from hermes_flaky_stabilization import config as unified_config

    unified_config.write_config({"pii": {"default_max_files": 123}},
                                _isolated_env / "flaky-stabilization")
    seen = {}

    def fake_scan(target, types, max_files):
        seen["max_files"] = max_files
        return {"success": True, "clean": True, "complete": True}

    monkeypatch.setattr(gate.scanner, "scan", fake_scan)
    clean = tmp_path / "clean.log"
    clean.write_text("all good\n", encoding="utf-8")
    result = gate.assert_evidence_clean([str(clean)])
    assert result.ok
    assert seen["max_files"] == 123


def test_gate_configured_max_files_bounds_the_walk(_isolated_env, tmp_path):
    """End to end: a directory with more files than pii.default_max_files is a
    truncated (incomplete) scan, so the gate fails closed."""
    from hermes_flaky_stabilization import config as unified_config

    unified_config.write_config({"pii": {"default_max_files": 1}},
                                _isolated_env / "flaky-stabilization")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "a.log").write_text("clean\n", encoding="utf-8")
    (evidence / "b.log").write_text("clean\n", encoding="utf-8")
    result = gate.assert_evidence_clean([str(evidence)])
    assert not result.ok
    assert str(evidence) in result.incomplete_paths


def test_gate_invalid_or_oversized_config_value_degrades_safely(_isolated_env,
                                                                monkeypatch, tmp_path):
    """A bad config value must not turn every scan into a validation error
    (which would fail the gate on clean evidence): invalid values fall back to
    the scanner default, oversized ones are clamped to the scanner ceiling."""
    from hermes_flaky_stabilization import config as unified_config
    from hermes_flaky_stabilization.pii import scanner as pii_scanner

    seen = []

    def fake_scan(target, types, max_files):
        seen.append(max_files)
        return {"success": True, "clean": True, "complete": True}

    monkeypatch.setattr(gate.scanner, "scan", fake_scan)
    clean = tmp_path / "clean.log"
    clean.write_text("all good\n", encoding="utf-8")

    unified_config.write_config({"pii": {"default_max_files": -5}},
                                _isolated_env / "flaky-stabilization")
    assert gate.assert_evidence_clean([str(clean)]).ok
    unified_config.write_config({"pii": {"default_max_files": 999_999}},
                                _isolated_env / "flaky-stabilization")
    assert gate.assert_evidence_clean([str(clean)]).ok
    assert seen == [None, pii_scanner.MAX_MAX_FILES]


# --- dedup (D9) ------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def search(self, query, limit=5):
        return list(self._rows)[:limit]


def test_dedup_ranks_by_token_overlap():
    rows = [
        {"key": "INC-1", "summary": "database pool exhausted in worker",
         "body": "", "status": "Open"},
        {"key": "INC-2", "summary": "checkout button broken on safari browser",
         "body": "", "status": "Open"},
    ]
    out = dedup.find_duplicate_incidents(_FakeStore(rows),
                                         "checkout broken on safari")
    assert out["count"] == 2
    assert out["candidates"][0]["key"] == "INC-2"
    assert out["candidates"][0]["score"] > out["candidates"][1]["score"]


def test_dedup_redacts_candidates():
    rows = [{"key": "INC-3", "summary": "outage reported by ops@corp.com",
             "body": "", "status": "Open"}]
    out = dedup.find_duplicate_incidents(_FakeStore(rows), "outage reported")
    assert "ops@corp.com" not in json.dumps(out)
    assert "[redacted-email]" in out["candidates"][0]["summary"]


def test_dedup_empty_inputs():
    assert dedup.find_duplicate_incidents(None, "x") == {"candidates": [], "count": 0}
    assert dedup.find_duplicate_incidents(_FakeStore([]), "") == {
        "candidates": [], "count": 0}


# --- jira_create_incident (D7) ---------------------------------------------------------


def _write_cfg(home, enable_write, base_url="https://x.atlassian.net"):
    from hermes_flaky_stabilization import config as unified_config

    unified_config.write_config(
        {"jira": {"enable_write": enable_write, "base_url": base_url,
                  "project_key": "INC", "issue_type": "Bug"}},
        home / "flaky-stabilization",
    )


def test_create_incident_refuses_when_write_disabled(_isolated_env, monkeypatch):
    from hermes_flaky_stabilization.incidents import write

    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    _write_cfg(_isolated_env, enable_write=False)
    out = json.loads(write.handle_create_incident({"title": "t", "body": "b"}))
    assert out["success"] is False and out["error"] == "tracker_write_disabled"


def test_create_incident_pii_gate_blocks_dirty_evidence(_isolated_env, monkeypatch,
                                                        tmp_path):
    from hermes_flaky_stabilization.incidents import write

    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    _write_cfg(_isolated_env, enable_write=True)
    dirty = tmp_path / "dirty.txt"
    dirty.write_text("mail alice@example.com\n", encoding="utf-8")
    out = json.loads(write.handle_create_incident(
        {"title": "t", "body": "b", "evidence_paths": [str(dirty)]}))
    assert out["success"] is False and out["error"] == "pii_gate_failed"
    assert out["findings_summary"].get("email") == 1
    assert "alice@example.com" not in json.dumps(out)


def test_create_incident_posts_redacted_fields(_isolated_env, monkeypatch):
    from hermes_flaky_stabilization.incidents import config as config_mod
    from hermes_flaky_stabilization.incidents import write
    from hermes_flaky_stabilization.incidents.jira_client import JiraClient

    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    _write_cfg(_isolated_env, enable_write=True)

    requests = []

    def fake_transport(method, url, headers, body, timeout):
        requests.append({"method": method, "url": url, "body": body})
        return 201, json.dumps({"id": "1", "key": "INC-99"}).encode()

    def build_client(cfg, token):
        return JiraClient(base_url="https://x.atlassian.net", token=token,
                          email="qa@example.com", transport=fake_transport)

    monkeypatch.setattr(config_mod, "build_client", build_client)
    out = json.loads(write.handle_create_incident({
        "title": "Outage seen by jane.doe@corp.com",
        "body": "Contact ops@corp.com; token ATATT3xFfQbADs12345_-=abcdef.",
        "severity": "high",
    }))
    assert out == {"success": True, "created": True, "key": "INC-99",
                   "url": "https://x.atlassian.net/browse/INC-99"}
    assert len(requests) == 1
    posted = requests[0]
    assert posted["method"] == "POST"
    assert posted["url"].endswith("/rest/api/3/issue")
    body_text = posted["body"].decode()
    assert "[redacted-email]" in body_text
    assert "jane.doe@corp.com" not in body_text
    assert "ops@corp.com" not in body_text
    assert "ATATT3xFfQbADs12345" not in body_text  # secrets scrubbed too


def test_create_incident_error_never_echoes_body(_isolated_env, monkeypatch):
    from hermes_flaky_stabilization.incidents import config as config_mod
    from hermes_flaky_stabilization.incidents import write
    from hermes_flaky_stabilization.incidents.jira_client import JiraClient

    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    _write_cfg(_isolated_env, enable_write=True)

    def failing_transport(method, url, headers, body, timeout):
        return 403, b'{"errorMessages": ["forbidden"]}'

    monkeypatch.setattr(
        config_mod, "build_client",
        lambda cfg, token: JiraClient(base_url="https://x.atlassian.net",
                                      token=token, transport=failing_transport))
    out_raw = write.handle_create_incident(
        {"title": "secret title xyzzy", "body": "secret body plugh"})
    out = json.loads(out_raw)
    assert out["success"] is False
    assert "xyzzy" not in out_raw and "plugh" not in out_raw


def test_create_incident_check_fn(_isolated_env, monkeypatch):
    from hermes_flaky_stabilization.incidents import write

    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    _write_cfg(_isolated_env, enable_write=True)
    assert not write.check_fn()          # no token
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    assert write.check_fn()              # token + enabled
    _write_cfg(_isolated_env, enable_write=False)
    assert not write.check_fn()          # token but disabled
