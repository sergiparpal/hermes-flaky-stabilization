"""Bootstrap + fakes for the ported hermes-bug-report-improver suite.

The legacy suite imports ``hermes_bug_report_improver``; alias that name to
the unified package's ``bugreport`` stage so the ported test file runs
unchanged. The fake ``ctx.llm`` classes below are verbatim from the legacy
conftest (they model ``PluginLlmStructuredResult`` with ``.parsed``/``.text``).
"""

from __future__ import annotations

import importlib
import sys

import pytest

_ALIAS = "hermes_bug_report_improver"
_REAL = "hermes_flaky_stabilization.bugreport"

_pkg = importlib.import_module(_REAL)
sys.modules.setdefault(_ALIAS, _pkg)
for _name in ("schema", "handler", "engine", "prompts", "domain",
              "rendering", "validation", "host"):
    _mod = importlib.import_module(f"{_REAL}.{_name}")
    sys.modules.setdefault(f"{_ALIAS}.{_name}", _mod)


class _FakeResult:
    """Mimics PluginLlmStructuredResult."""

    def __init__(self, parsed=None, text="", provider="mock", model="mock-model"):
        self.parsed = parsed
        self.text = text
        self.provider = provider
        self.model = model
        self.content_type = "json" if parsed is not None else "text"


class _FakeLLM:
    """Configurable fake ``ctx.llm``.

    ``responses`` is consumed one item per ``complete_structured`` call. Each item
    may be a dict (-> parsed JSON), ``None`` (-> unparseable), an ``Exception``
    instance (-> raised), or a ``_FakeResult``.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError(
                "complete_structured called more times than configured responses"
            )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, _FakeResult):
            return item
        return _FakeResult(parsed=item)


class _FakeCtx:
    def __init__(self, llm):
        self.llm = llm


@pytest.fixture
def mock_ctx():
    """Factory fixture: ``mock_ctx([dict|None|Exception|result, ...])`` -> ctx."""

    def _make(responses=None):
        return _FakeCtx(_FakeLLM(responses or []))

    return _make


@pytest.fixture
def llm_result():
    """Factory for a raw result object (when a test needs to set ``.text``)."""

    def _make(parsed=None, text=""):
        return _FakeResult(parsed=parsed, text=text)

    return _make


@pytest.fixture
def no_llm_ctx():
    """A ctx whose ``llm`` attribute is None (ctx.llm unavailable)."""
    return _FakeCtx(None)
