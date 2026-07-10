"""Bootstrap + fixtures for the ported hermes-ci-triage suite.

The legacy suite imports ``hermes_plugins.hermes_ci_triage`` (the fully
qualified name Hermes gives a directory plugin). Alias that name to the
unified package's ``triage`` stage so the ported test files run unchanged.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

_NS_PARENT = "hermes_plugins"
_FQ = "hermes_plugins.hermes_ci_triage"
_REAL = "hermes_flaky_stabilization.triage"

if _NS_PARENT not in sys.modules:
    ns = types.ModuleType(_NS_PARENT)
    ns.__path__ = []  # namespace package
    ns.__package__ = _NS_PARENT
    sys.modules[_NS_PARENT] = ns

_pkg = importlib.import_module(_REAL)
sys.modules.setdefault(_FQ, _pkg)
sys.modules[_NS_PARENT].hermes_ci_triage = _pkg
for _name in ("classifier", "enrichment", "handlers", "logfetch", "patterns",
              "ports", "prefilter", "redact", "safehttp", "taxonomy"):
    _mod = importlib.import_module(f"{_REAL}.{_name}")
    sys.modules.setdefault(f"{_FQ}.{_name}", _mod)

PLUGIN_PACKAGE = _pkg


@pytest.fixture(autouse=True)
def isolated_unified_config(tmp_path, monkeypatch):
    """Point the unified config at an empty temp data dir for every test.

    ``logfetch``/``safehttp`` now consult ``<data_dir>/config.json`` for
    ``triage.log_roots`` / ``token_hosts`` / ``allow_private`` when the
    HERMES_CI_TRIAGE_* env vars are unset — a real operator config must never
    leak into (or be created by) the suite. Config-wiring tests write their
    config.json into the returned directory.
    """
    from hermes_flaky_stabilization import paths

    cfg_dir = tmp_path / "flaky-stab-config"
    cfg_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "get_data_dir", lambda: cfg_dir)
    return cfg_dir


@pytest.fixture
def tmp_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME (and Path.home) at a temp dir so SQLite lands there."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Pin the resolver directly so DB isolation is guaranteed: handler tests call
    # the pipeline without an injected hermes_home, and this makes the SQLite file
    # land in the temp dir regardless of how HERMES_HOME would otherwise resolve —
    # never the operator's real pattern DB. (Handlers no longer imports Hermes;
    # the profile-aware resolution lives in __init__.register.)
    from hermes_plugins.hermes_ci_triage import handlers
    monkeypatch.setattr(handlers, "_resolve_hermes_home", lambda hermes_home=None: home)
    return home


@pytest.fixture
def seeded_history(tmp_hermes_home):
    """A test-history DB (inside the tmp home) with one known failing test,
    for the fixed direct-call enrichment path (plan §2 row 6)."""
    from hermes_flaky_stabilization.history import storage as history_storage

    history_storage.reset_for_tests()
    conn = history_storage.get_connection()
    cur = conn.execute(
        "INSERT INTO test_runs (suite_name, run_timestamp, total, failures, errors,"
        " skipped, source_file) VALUES (?, ?, 1, 1, 0, 0, ?)",
        ("enrich-suite", "2026-07-01T09:00:00", "enrich::run1"),
    )
    conn.execute(
        "INSERT INTO test_cases (run_id, classname, name, file_path, status,"
        " failure_message, failure_type, stack_trace) VALUES (?,?,?,?,?,?,?,?)",
        (cur.lastrowid, "shop.checkout", "test_checkout_flow",
         "src/shop/test_checkout.py", "failed",
         "assert failed: checkout flaky", "AssertionError", "Trace..."),
    )
    conn.commit()
    yield conn
    history_storage.reset_for_tests()
