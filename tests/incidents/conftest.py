"""Bootstrap + fixtures for the ported hermes-jira-incidents suite.

The legacy suite imports ``jira_incidents``; alias that name to the unified
package's ``incidents`` stage so the ported test files run unchanged (the
``redaction`` submodule maps onto the ``pii`` stage, where the module now
lives). The legacy conftest also required a Hermes checkout for the
memory-slot ABC — no longer: the service is Hermes-free, so the whole suite
runs offline with no skip gate.
"""

from __future__ import annotations

import importlib
import sys

import pytest

_ALIAS = "jira_incidents"
_REAL = "hermes_flaky_stabilization.incidents"

_pkg = importlib.import_module(_REAL)
_redaction = importlib.import_module("hermes_flaky_stabilization.pii.redaction")
if not isinstance(sys.modules.get(_ALIAS), type(_pkg)) or sys.modules.get(_ALIAS) is not _pkg:
    sys.modules[_ALIAS] = _pkg
for _name in ("jira_client", "ingest", "store", "prefetch", "sync", "config", "cli"):
    sys.modules[f"{_ALIAS}.{_name}"] = importlib.import_module(f"{_REAL}.{_name}")
sys.modules[f"{_ALIAS}.redaction"] = _redaction


@pytest.fixture
def tmp_home(tmp_path) -> str:
    """A throwaway HERMES_HOME for profile-scoped storage."""
    home = tmp_path / "hermes_home"
    home.mkdir(parents=True, exist_ok=True)
    return str(home)
