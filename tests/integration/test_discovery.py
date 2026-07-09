"""The unified plugin under the REAL PluginManager (plan Phase 8 acceptance)."""

from __future__ import annotations

from intg_helpers import PLUGIN_NAME, install_plugin, run_discovery

EXPECTED_TOOLSETS = {
    "test_failure_lookup": "test_history",
    "module_failure_history": "test_history",
    "is_flaky": "flaky_detective",
    "triage_pipeline_failure": "ci_triage",
    "fetch_ci_logs": "flaky_healer",
    "analyze_playwright_trace": "flaky_healer",
    "heal_flaky_test": "flaky_healer",
    "list_healing_recipes": "flaky_healer",
    "improve_bug_report": "qa",
    "validate_no_pii": "qa_masking",
    "jira_search_incident": "jira_incidents",
    "jira_get_root_cause": "jira_incidents",
    "jira_link_session": "jira_incidents",
    "jira_create_incident": "jira_incidents",
    "stabilize_test_failure": "flaky_stabilization",
    "find_duplicate_incidents": "flaky_stabilization",
}
EXPECTED_HOOKS = {"pre_tool_call", "pre_llm_call", "on_session_start",
                  "pre_approval_request", "post_approval_response"}


def test_plugin_loads_and_registers_full_surface(hermes_home):
    install_plugin(hermes_home)
    out = run_discovery(hermes_home)

    assert out["found"], "plugin was not discovered"
    assert out["enabled"], f"plugin failed to load: {out['error']!r}"
    assert out["error"] in (None, "")

    # All 16 tools in the real tools.registry, under their original toolsets.
    assert set(out["tools_registered"]) == set(EXPECTED_TOOLSETS)
    assert out["toolsets"] == EXPECTED_TOOLSETS

    # The 5 hooks (other/bundled plugins may add more names; ours must be in).
    assert EXPECTED_HOOKS <= set(out["hooks"])

    # Skill under the qualified name; both CLI commands.
    assert f"{PLUGIN_NAME}:flaky-healer" in out["skills"]
    assert {"flaky-stab", "test-history"} <= set(out["cli_commands"])


def test_broken_submodule_fails_the_load_naming_the_module(hermes_home):
    dest = install_plugin(hermes_home)
    (dest / "hermes_flaky_stabilization" / "pii" / "scanner.py").write_text(
        "import totally_missing_module_xyz\n", encoding="utf-8"
    )
    out = run_discovery(hermes_home)
    assert out["found"]
    assert not out["enabled"], "a plugin with a broken import must not load"
    assert "totally_missing_module_xyz" in (out["error"] or ""), (
        f"load error must name the real missing module, got {out['error']!r}"
    )
    assert out["tools_registered"] == []
