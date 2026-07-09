"""Frozen tool-schema snapshots (plan §8 layer 2).

Every registered tool's OpenAI-style schema (and its toolset) must deep-equal
the JSON snapshot checked into ``tests/snapshots/`` — the snapshots were dumped
from the *legacy* plugins' source, so equality here proves the byte-level
contract survived the port. New (net-new) tools get their own snapshots when
they land; regenerating a snapshot is a deliberate, reviewed act.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _doubles import FakePluginContext

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def _registered_tools() -> dict:
    import hermes_flaky_stabilization as plugin

    ctx = FakePluginContext()
    plugin.register(ctx)
    return ctx.tools


def _snapshot_names() -> list[str]:
    return sorted(p.stem for p in SNAPSHOT_DIR.glob("*.json"))


def test_every_registered_tool_has_a_snapshot():
    tools = _registered_tools()
    assert sorted(tools) == _snapshot_names()


@pytest.mark.parametrize("name", _snapshot_names())
def test_schema_matches_snapshot(name):
    tools = _registered_tools()
    assert name in tools, f"tool {name} not registered"
    snap = json.loads((SNAPSHOT_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert tools[name]["toolset"] == snap["toolset"]
    assert tools[name]["schema"] == snap["schema"]
