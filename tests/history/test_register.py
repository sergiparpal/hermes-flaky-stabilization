"""Plugin registration wires the two tools and the CLI command.

These tests do not require a live Hermes: a ``FakeCtx`` records what
``register`` would have registered, which is enough to assert tool names,
toolset, and the CLI command — and to exercise a registered handler end to end.
"""

import json

from hermes_test_history import (
    MODULE_FAILURE_HISTORY_SCHEMA,
    TEST_FAILURE_LOOKUP_SCHEMA,
    register,
)


class FakeCtx:
    """Minimal stand-in for Hermes's PluginContext that records registrations."""

    def __init__(self):
        self.tools = {}
        self.cli_commands = {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            **kwargs,
        }

    def register_cli_command(self, name, help, setup_fn, handler_fn, **kwargs):
        self.cli_commands[name] = {
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
        }


def test_register_wires_both_tools():
    ctx = FakeCtx()
    register(ctx)
    assert set(ctx.tools) == {"test_failure_lookup", "module_failure_history"}
    for tool in ctx.tools.values():
        assert tool["toolset"] == "test_history"
        assert callable(tool["handler"])


def test_register_wires_cli_command():
    ctx = FakeCtx()
    register(ctx)
    assert "test-history" in ctx.cli_commands
    cmd = ctx.cli_commands["test-history"]
    assert callable(cmd["setup_fn"])
    assert callable(cmd["handler_fn"])


def test_tool_schemas_names_and_required_fields():
    assert TEST_FAILURE_LOOKUP_SCHEMA["name"] == "test_failure_lookup"
    assert TEST_FAILURE_LOOKUP_SCHEMA["parameters"]["required"] == ["test_id"]
    assert MODULE_FAILURE_HISTORY_SCHEMA["name"] == "module_failure_history"
    assert MODULE_FAILURE_HISTORY_SCHEMA["parameters"]["required"] == ["path"]
    # Schemas must be JSON-serializable to reach the model.
    json.dumps(TEST_FAILURE_LOOKUP_SCHEMA)
    json.dumps(MODULE_FAILURE_HISTORY_SCHEMA)


def test_registered_handler_returns_json_string(profile_env):
    ctx = FakeCtx()
    register(ctx)
    handler = ctx.tools["test_failure_lookup"]["handler"]
    payload = json.loads(handler({"test_id": "nonexistent"}))  # must be a JSON string
    assert payload["success"] is True
    assert payload["total_runs"] == 0
