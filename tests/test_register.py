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

# Phase-1 surface: nothing but the CLI command.
EXPECTED_TOOLS: set[str] = set()
EXPECTED_HOOKS: set[str] = set()
EXPECTED_COMMANDS: set[str] = set()
EXPECTED_CLI_COMMANDS = {"flaky-stab"}
EXPECTED_SKILLS: set[str] = set()


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

captured = {}
class Ctx:
    llm = None
    def register_cli_command(self, **kw):
        captured.update(kw)
    def register_tool(self, **kw):
        pass
    def register_hook(self, *a, **kw):
        pass
    def register_command(self, **kw):
        pass
    def register_skill(self, *a, **kw):
        pass

mod.register(Ctx())
assert captured.get("name") == "flaky-stab", captured
assert callable(captured.get("setup_fn")), captured
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
