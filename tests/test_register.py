"""Registration surface tests (Phase 1: CLI skeleton only).

The exact-registry snapshot grows with each phase; keeping it exact (not
subset-based) is what catches accidental surface drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _doubles import FakePluginContext, load_plugin_module

REPO_ROOT = Path(__file__).resolve().parent.parent

# The complete surface (plan §6.2): 13 preserved tools + 3 net-new, 5 hooks,
# 3 slash commands, 2 CLI commands, 1 skill.
EXPECTED_TOOLS: set[str] = {
    "test_failure_lookup",
    "module_failure_history",
    "is_flaky",
    "triage_pipeline_failure",
    "fetch_ci_logs",
    "analyze_playwright_trace",
    "heal_flaky_test",
    "list_healing_recipes",
    "improve_bug_report",
    "validate_no_pii",
    "jira_search_incident",
    "jira_get_root_cause",
    "jira_link_session",
    "jira_create_incident",
    "stabilize_test_failure",
    "find_duplicate_incidents",
}
EXPECTED_HOOKS: set[str] = {
    "pre_approval_request",
    "post_approval_response",
    "pre_llm_call",
    "on_session_start",
    "pre_tool_call",
}
EXPECTED_COMMANDS: set[str] = {"improve-bug", "heal", "stabilize"}
EXPECTED_CLI_COMMANDS = {"flaky-stab", "test-history"}
EXPECTED_SKILLS: set[str] = {"flaky-healer"}

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


def _register(ctx=None):
    import hermes_flaky_stabilization as plugin

    ctx = ctx or FakePluginContext()
    plugin.register(ctx)
    return ctx


def test_register_completes_and_matches_snapshot():
    ctx = _register()
    assert set(ctx.tools) == EXPECTED_TOOLS
    assert set(ctx.hooks) == EXPECTED_HOOKS
    assert set(ctx.commands) == EXPECTED_COMMANDS
    assert set(ctx.cli_commands) == EXPECTED_CLI_COMMANDS
    assert set(ctx.skills) == EXPECTED_SKILLS


def test_tools_keep_their_original_toolsets():
    ctx = _register()
    for name, toolset in EXPECTED_TOOLSETS.items():
        assert ctx.tools[name]["toolset"] == toolset, name


def test_register_twice_on_fresh_contexts_is_idempotent():
    first = _register()
    second = _register()
    assert set(first.cli_commands) == set(second.cli_commands) == EXPECTED_CLI_COMMANDS


def test_cli_command_is_wired():
    ctx = _register()
    entry = ctx.cli_commands["flaky-stab"]
    assert callable(entry["setup_fn"])
    assert callable(entry["handler_fn"])
    assert entry["help"]


def test_loaded_like_hermes_registers_the_same_surface():
    module = load_plugin_module()
    ctx = FakePluginContext()
    module.register(ctx)
    assert set(ctx.cli_commands) == EXPECTED_CLI_COMMANDS


# --- kind auto-detection guard (plan §3.2) --------------------------------------
# The loader scans the first 8 KiB of the plugin root __init__.py and coerces the
# plugin to the memory-exclusive kind if it sees either marker string. Both entry
# files must stay clean of them forever.

_FORBIDDEN_MARKERS = ("register_memory" + "_provider", "Memory" + "Provider")


def test_entry_files_free_of_memory_provider_markers():
    package_init = REPO_ROOT / "hermes_flaky_stabilization" / "__init__.py"
    for entry in (REPO_ROOT / "__init__.py", package_init):
        head = entry.read_text(encoding="utf-8")[:8192]
        for marker in _FORBIDDEN_MARKERS:
            assert marker not in head, f"{entry}: forbidden marker {marker!r} (kind coercion)"


# --- Hermes compatibility shim (root __init__.py) ------------------------------
# Hermes loads a plugin by executing the root __init__.py via importlib with
# submodule_search_locations, NOT by putting the (hyphenated) plugin directory on
# sys.path. Reproduce that load from a foreign CWD with PYTHONPATH cleared and
# the package not installed, so it fails if the shim stops making
# `hermes_flaky_stabilization` importable on its own.

_HERMES_LOADER = """\
import importlib.util, sys
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "flaky_stabilization_plugin",
    root / "__init__.py",
    submodule_search_locations=[str(root)],
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

cli, tools = {}, set()
class Ctx:
    llm = None
    def register_cli_command(self, *a, **kw):
        name = kw.get("name", a[0] if a else None)
        cli[name] = kw.get("setup_fn", a[2] if len(a) > 2 else None)
    def register_tool(self, *a, **kw):
        tools.add(kw.get("name", a[0] if a else None))
    def register_hook(self, *a, **kw):
        pass
    def register_command(self, *a, **kw):
        pass
    def register_skill(self, *a, **kw):
        pass

mod.register(Ctx())
assert {"flaky-stab", "test-history"} <= set(cli), cli
assert callable(cli["flaky-stab"]), cli
assert {"test_failure_lookup", "module_failure_history", "is_flaky"} <= tools, tools
print("SHIM_OK")
"""


def test_root_shim_loads_the_way_hermes_does(tmp_path):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # don't let an inherited path import the package for us
    proc = subprocess.run(
        [sys.executable, "-c", _HERMES_LOADER, str(REPO_ROOT)],
        cwd=str(tmp_path),  # a CWD that is NOT the plugin dir
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SHIM_OK" in proc.stdout
