"""Shared test doubles for hermes-flaky-stabilization.

``FakePluginContext`` mirrors the subset of the Hermes Agent
``PluginContext`` API this plugin uses (plan §3.3), extended from
hermes-flaky-healer's proven double with CLI-command recording and hook
firing. The whole default suite runs offline: no Hermes installation, no
network, no live LLM — everything goes through the doubles here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Mirrors VALID_HOOKS from hermes-agent 0.18.2 (hermes_cli/plugins.py) — 23 hooks.
VALID_HOOKS = frozenset(
    {
        "pre_tool_call",
        "post_tool_call",
        "transform_terminal_output",
        "transform_tool_result",
        "transform_llm_output",
        "pre_llm_call",
        "post_llm_call",
        "pre_verify",
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "on_session_start",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
        "subagent_start",
        "subagent_stop",
        "pre_gateway_dispatch",
        "pre_approval_request",
        "post_approval_response",
        "kanban_task_claimed",
        "kanban_task_completed",
        "kanban_task_blocked",
    }
)


class FakeLLM:
    """Stub for ``ctx.llm``. Queue responses; each structured call pops one.

    Queued items are returned as-is, so a stage that expects a plain dict
    (healer style) queues dicts, and a stage that expects a result object with
    ``.parsed`` / ``.text`` queues such an object.
    """

    def __init__(self):
        self.queued: list = []
        self.calls: list[dict] = []

    def queue(self, response) -> None:
        self.queued.append(response)

    def complete(self, *args, **kwargs) -> str:
        self.calls.append({"kind": "complete", "args": args, "kwargs": kwargs})
        return "stub-completion"

    def complete_structured(self, **kwargs):
        self.calls.append({"kind": "complete_structured", "kwargs": kwargs})
        if not self.queued:
            raise AssertionError("FakeLLM: no queued structured response")
        item = self.queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def structured_calls(self) -> list[dict]:
        return [c for c in self.calls if c["kind"] == "complete_structured"]


class FakePluginContext:
    """Records every registration; dispatch_tool records and returns canned results."""

    def __init__(self, hermes_home: Path | str | None = None):
        self.tools: dict[str, dict] = {}
        self.hooks: dict[str, list] = {}
        self.skills: dict[str, str] = {}
        self.commands: dict[str, dict] = {}
        self.cli_commands: dict[str, dict] = {}
        self.dispatched: list[dict] = []
        # tool name -> canned result (or callable(arguments) -> result)
        self.dispatch_results: dict[str, object] = {}
        # optional callable(name, arguments) -> result; takes precedence when set
        self.dispatch_executor = None
        self.llm = FakeLLM()
        self.profile_name = "test-profile"
        if hermes_home is not None:
            self.hermes_home = str(hermes_home)

    # --- registration surface (signatures per plan §3.3) ---

    def register_tool(
        self,
        name,
        toolset=None,
        schema=None,
        handler=None,
        check_fn=None,
        requires_env=None,
        is_async=False,
        description="",
        emoji="",
        override=False,
    ):
        assert isinstance(name, str) and name, "tool name must be a non-empty string"
        assert callable(handler), f"handler for {name} must be callable"
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "requires_env": requires_env,
            "is_async": is_async,
            "description": description,
            "emoji": emoji,
            "override": override,
        }

    def register_hook(self, hook_name, callback):
        assert hook_name in VALID_HOOKS, f"unknown hook name: {hook_name}"
        assert callable(callback)
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_skill(self, name, path, description=""):
        assert Path(path).is_file(), f"skill file missing: {path}"
        self.skills[name] = str(path)

    def register_command(self, name, handler, description="", args_hint=""):
        assert callable(handler)
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        assert isinstance(name, str) and name
        assert callable(setup_fn)
        self.cli_commands[name] = {
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
            "description": description,
        }

    # --- runtime surface ---

    def dispatch_tool(self, name, arguments, **kwargs):
        self.dispatched.append({"tool": name, "arguments": arguments, "kwargs": kwargs})
        if self.dispatch_executor is not None:
            return self.dispatch_executor(name, arguments)
        result = self.dispatch_results.get(name, {"status": "ok"})
        return result(arguments) if callable(result) else result

    # --- test helpers ---

    def fire_hook(self, hook_name, *args, **kwargs):
        """Invoke registered callbacks the way the host would (all fire; a
        raising callback is swallowed to WARNING in the real host — here it
        propagates so tests catch bugs)."""
        return [cb(*args, **kwargs) for cb in self.hooks.get(hook_name, [])]


class FakeTransport:
    """HTTP double: maps (METHOD, url) -> (status, headers, body); records requests."""

    def __init__(self, routes: dict | None = None):
        self.routes: dict[tuple[str, str], tuple] = dict(routes or {})
        self.requests: list[dict] = []

    def add(self, method: str, url: str, body, status: int = 200, headers: dict | None = None):
        self.routes[(method.upper(), url)] = (status, headers or {}, body)

    def request(self, method, url, headers=None, body=None, timeout=30):
        self.requests.append(
            {
                "method": method.upper(),
                "url": url,
                "headers": dict(headers or {}),
                "body": body,
            }
        )
        try:
            status, resp_headers, payload = self.routes[(method.upper(), url)]
        except KeyError:
            return 404, {}, b'{"message": "Not Found (FakeTransport)"}'
        if isinstance(payload, dict | list):
            payload = json.dumps(payload).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        return status, dict(resp_headers), payload


def load_plugin_module(name: str = "hermes_flaky_stabilization_plugin"):
    """Load the plugin entry (root ``__init__.py``) the way a Hermes host would:
    via importlib with ``submodule_search_locations``, not via ``sys.path``."""
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        name,
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def find_hermes_repo() -> Path | None:
    """Locate a hermes-agent checkout for integration tests (skip when absent)."""
    candidates = []
    env = os.environ.get("HERMES_REPO")
    if env:
        candidates.append(Path(env))
    candidates += [
        REPO_ROOT.parent / "hermes-agent",
        Path.home() / "hermes-agent",
        Path.home() / "hermes" / "hermes-agent",
    ]
    for c in candidates:
        if c and (c / "hermes_cli" / "plugins.py").exists():
            return c
    return None
