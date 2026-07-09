"""Shared test doubles for the ported hermes-flaky-healer suite.

FakePluginContext mirrors the subset of the Hermes Agent ``PluginContext``
API this stage uses. The whole default suite runs offline: no Hermes
installation, no network, no live LLM — everything goes through the doubles
defined here.

Unified-plugin bootstrap: the legacy suite imports ``flaky_healer.*`` /
``handlers`` / ``schemas`` as top-level names and ``from conftest import``
helpers. Alias all of those onto the unified ``healer`` stage so the ported
test files run unchanged; this directory goes first on ``sys.path`` so
``import conftest`` resolves to THIS file (the legacy repo relied on the same
prepend behavior).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent.parent
FIXTURES = TESTS_DIR / "fixtures"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

_REAL = "hermes_flaky_stabilization.healer"
_pkg = importlib.import_module(_REAL)
sys.modules.setdefault("handlers", importlib.import_module(f"{_REAL}.handlers"))
sys.modules.setdefault("schemas", importlib.import_module(f"{_REAL}.schemas"))
sys.modules.setdefault("flaky_healer", importlib.import_module(f"{_REAL}.flaky_healer"))
for _name in (
    "config", "diagnose", "gitflow", "healer", "logfilter", "shapes",
    "skill_export", "trace", "zipsafe",
    "ci", "ci.base", "ci.github_actions",
    "recipes", "recipes.matcher", "recipes.signature", "recipes.store",
    "sandbox", "sandbox.base", "sandbox.docker", "sandbox.subproc",
    "strategies", "strategies.base", "strategies.await_state",
    "strategies.bump_timeout", "strategies.testid_selector",
):
    _mod = importlib.import_module(f"{_REAL}.flaky_healer.{_name}")
    sys.modules.setdefault(f"flaky_healer.{_name}", _mod)

# Mirrors VALID_HOOKS from the Hermes plugin contract (plan §3.4).
VALID_HOOKS = frozenset(
    {
        "pre_tool_call",
        "post_tool_call",
        "pre_llm_call",
        "post_llm_call",
        "on_session_start",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
        "transform_terminal_output",
        "transform_tool_result",
        "transform_llm_output",
        "subagent_stop",
        "pre_gateway_dispatch",
        "pre_approval_request",
        "post_approval_response",
    }
)


class FakeLLM:
    """Stub for ``ctx.llm``. Queue responses; each structured call pops one."""

    def __init__(self):
        self.queued: list[dict] = []
        self.calls: list[dict] = []

    def queue(self, response: dict) -> None:
        self.queued.append(response)

    def complete(self, *args, **kwargs) -> str:
        self.calls.append({"kind": "complete", "args": args, "kwargs": kwargs})
        return "stub-completion"

    def complete_structured(self, *, instructions: str, input, schema=None, **kwargs) -> dict:
        self.calls.append(
            {
                "kind": "complete_structured",
                "instructions": instructions,
                "input": input,
                "schema": schema,
                "kwargs": kwargs,
            }
        )
        if not self.queued:
            raise AssertionError("FakeLLM: no queued structured response")
        return self.queued.pop(0)

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
        self.dispatched: list[dict] = []
        # tool name -> canned result (or callable(arguments) -> result)
        self.dispatch_results: dict[str, object] = {}
        # optional callable(name, arguments) -> result; takes precedence when set
        self.dispatch_executor = None
        self.llm = FakeLLM()
        if hermes_home is not None:
            self.hermes_home = str(hermes_home)

    # --- registration surface (signatures per plan §3.3) ---

    def register_tool(self, name, toolset=None, schema=None, handler=None, check_fn=None):
        assert isinstance(name, str) and name, "tool name must be a non-empty string"
        assert callable(handler), f"handler for {name} must be callable"
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
        }

    def register_hook(self, hook_name, callback):
        assert hook_name in VALID_HOOKS, f"unknown hook name: {hook_name}"
        assert callable(callback)
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_skill(self, name, path):
        assert Path(path).is_file(), f"skill file missing: {path}"
        self.skills[name] = str(path)

    def register_command(self, name, handler, description=""):
        assert callable(handler)
        self.commands[name] = {"handler": handler, "description": description}

    # --- runtime surface ---

    def dispatch_tool(self, name, arguments):
        self.dispatched.append({"tool": name, "arguments": arguments})
        if self.dispatch_executor is not None:
            return self.dispatch_executor(name, arguments)
        result = self.dispatch_results.get(name, {"status": "ok"})
        return result(arguments) if callable(result) else result

    # --- test helpers ---

    def fire_hook(self, hook_name, *args, **kwargs):
        """Invoke registered callbacks the way the host would (observers)."""
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


def load_plugin_module(name: str = "hermes_flaky_healer"):
    """The healer stage entry (unified-plugin adaptation: the legacy repo-root
    ``__init__.py`` became the ``healer`` subpackage; its ``register()``
    surface is identical)."""
    return importlib.import_module(_REAL)


TOY_APP = FIXTURES / "toy-app"


def make_run_report(repeats: int, failures: int, isolation: str = "fake"):
    """Build a synthetic sandbox RunReport (lazy import: module lands in Phase 3)."""
    from flaky_healer.sandbox.base import RunResult, aggregate

    results = [
        RunResult(exit_code=1 if i < failures else 0, duration_s=0.01) for i in range(repeats)
    ]
    report = aggregate(results, isolation)
    return report


class FakeSandbox:
    """Scripted sandbox double: pop a queued RunReport per run_test call.

    With an empty queue it returns all-green reports, so 'reproduce fails,
    burn-in passes' scenarios queue exactly the reports they need.
    """

    isolation = "fake"

    def __init__(self, scripted=None):
        self.scripted = list(scripted or [])
        self.calls: list[dict] = []

    def run_test(self, repo_dir, test_id, repeats=1, timeout_s=180, prepare=None):
        self.calls.append(
            {
                "repo_dir": str(repo_dir),
                "test_id": test_id,
                "repeats": repeats,
                "prepared": prepare is not None,
            }
        )
        if self.scripted:
            return self.scripted.pop(0)
        return make_run_report(repeats, failures=0, isolation=self.isolation)


def build_synthetic_trace(
    path: Path,
    *,
    selector: str = "#widget",
    error_name: str = "Error",
    error_message: str = "widget desync (code 0xWAT): rendered checksum mismatch",
    method: str = "click",
    timed_out: bool = False,
    call_log: str = 'locator resolved to <div id="widget">…</div>',
) -> Path:
    """Minimal v8-shaped trace.zip for synthetic failure scenarios in tests."""
    import zipfile as _zipfile

    events = [
        {
            "version": 8,
            "type": "context-options",
            "origin": "library",
            "browserName": "chromium",
            "options": {},
            "platform": "linux",
            "wallTime": 1781270000100,
            "monotonicTime": 1100.0,
            "sdkLanguage": "javascript",
        },
        {
            "type": "before",
            "callId": "call@10",
            "startTime": 1200.0,
            "class": "Frame",
            "method": method,
            "params": {"selector": selector, "strict": True},
            "pageId": "page@1",
        },
        {"type": "log", "callId": "call@10", "time": 1210.0, "message": call_log},
        {
            "type": "after",
            "callId": "call@10",
            "endTime": 1300.0,
            "error": {
                "name": "",
                "message": error_message,
                "stack": f"{error_name}: {error_message}\n    at x (/repo/x.js:1:1)",
            },
            "result": {"timedOut": timed_out},
        },
    ]
    with _zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0-trace.trace", "\n".join(json.dumps(e) for e in events) + "\n")
    return path


def local_browsers_available() -> bool:
    """True when the subprocess backend can actually launch Chromium here."""
    import shutil as _shutil

    if _shutil.which("node") is None or not (TOY_APP / "node_modules").is_dir():
        return False
    browsers = Path.home() / ".cache" / "ms-playwright"
    return browsers.is_dir() and any(browsers.glob("chromium*"))


@pytest.fixture
def browser_env(monkeypatch):
    """Host adaptation: this WSL box keeps Chromium's missing system libraries
    in ~/.local/pw-syslibs (no sudo available); expose them to the subprocess
    sandbox through its documented env allowlist."""
    libs = Path.home() / ".local" / "pw-syslibs"
    if libs.is_dir():
        monkeypatch.setenv("LD_LIBRARY_PATH", str(libs))
        monkeypatch.setenv("FLAKY_HEALER_SUBPROC_PASS_ENV", "LD_LIBRARY_PATH")
    return libs


@pytest.fixture
def plugin_module():
    return load_plugin_module()


@pytest.fixture
def fake_ctx(tmp_path):
    return FakePluginContext(hermes_home=tmp_path / "hermes-home")


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """Deterministic env: clear plugin vars + GITHUB_TOKEN, isolate the data dir."""
    for var in list(os.environ):
        if var.startswith("FLAKY_HEALER_") or var == "GITHUB_TOKEN":
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FLAKY_HEALER_DATA_DIR", str(tmp_path / "healer-data"))
