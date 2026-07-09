"""Shared helpers for the real-core integration tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _doubles import find_hermes_repo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_NAME = "hermes-flaky-stabilization"
HERMES_REPO = find_hermes_repo()


def install_plugin(home: Path) -> Path:
    """Copy the plugin into ``<home>/plugins/`` and opt it in via config.yaml."""
    dest = home / "plugins" / PLUGIN_NAME
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "plugin.yaml", dest / "plugin.yaml")
    shutil.copy(REPO_ROOT / "__init__.py", dest / "__init__.py")
    shutil.copytree(
        REPO_ROOT / "hermes_flaky_stabilization",
        dest / "hermes_flaky_stabilization",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(REPO_ROOT / "skills", dest / "skills")
    (dest / "scripts").mkdir(exist_ok=True)
    for shim in (REPO_ROOT / "scripts").glob("*.sh"):
        shutil.copy(shim, dest / "scripts" / shim.name)
    # Opt in (standalone plugins load only when listed in plugins.enabled).
    (home / "config.yaml").write_text(
        f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n", encoding="utf-8"
    )
    return dest


DRIVER = r"""
import json, sys
hermes_repo, = sys.argv[1:]
sys.path.insert(0, hermes_repo)
from hermes_cli.plugins import PluginManager
from tools.registry import registry

pm = PluginManager()
pm.discover_and_load()
loaded = pm._plugins.get("hermes-flaky-stabilization")
tool_names = registry.get_all_tool_names()
out = {
    "found": loaded is not None,
    "enabled": bool(loaded and loaded.enabled),
    "error": (getattr(loaded, "error", None) or None) if loaded else None,
    "tools_registered": sorted(getattr(loaded, "tools_registered", []) or []),
    "toolsets": {
        name: registry.get_toolset_for_tool(name)
        for name in (getattr(loaded, "tools_registered", []) or [])
        if name in tool_names
    },
    "hooks": sorted(pm._hooks.keys()),
    "skills": sorted(pm._plugin_skills.keys()),
    "cli_commands": sorted(pm._cli_commands.keys()),
}
print("JSON:" + json.dumps(out))
"""


def run_discovery(home: Path, extra_env: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["HERMES_HOME"] = str(home)
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, str(HERMES_REPO)],
        capture_output=True, text=True, env=env, cwd=str(home), timeout=180,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr}\n{proc.stdout}"
    for line in proc.stdout.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    raise AssertionError(f"driver printed no JSON:\n{proc.stdout}\n{proc.stderr}")
