"""Bootstrap + shared fixtures for the ported hermes-test-history suite.

The legacy suite imports ``hermes_test_history``; alias that name to the
unified package's ``history`` stage so every ported test file runs unchanged
(the sanctioned port edit is confined to this conftest). The alias reuses the
*same* module objects, so the module-level singletons in ``storage.py`` (the
connection and config caches) stay single-instance across both the stage's
internal relative imports and the tests' aliased imports.
"""

import importlib
import sys
from pathlib import Path

import pytest

_ALIAS = "hermes_test_history"
_REAL = "hermes_flaky_stabilization.history"

_pkg = importlib.import_module(_REAL)
sys.modules.setdefault(_ALIAS, _pkg)
for _name in ("schema", "timeutil", "domain", "parser", "ingest", "storage", "queries", "cli"):
    _mod = importlib.import_module(f"{_REAL}.{_name}")
    sys.modules.setdefault(f"{_ALIAS}.{_name}", _mod)

from hermes_test_history import domain, storage  # noqa: E402  (after bootstrap)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------
# Fixtures (verbatim from the legacy suite)
# --------------------------------------------------------------------------


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME under a tmp dir and reset storage singletons.

    Mirrors catalog §5.5: monkeypatch ``Path.home`` and ``HERMES_HOME`` so the
    plugin's profile-aware home resolves inside the tmp dir, never the real one.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    storage.reset_for_tests()
    yield home
    storage.reset_for_tests()


@pytest.fixture
def db(profile_env):
    """An empty database with the schema applied, ready for ingest/query."""
    return storage.get_connection()


@pytest.fixture
def seeded_db(db):
    """A database seeded with a small, deterministic dataset.

    Three runs of an auth + billing suite, with ``test_oauth_login`` failing in
    the first two runs and passing in the third. Used by query tests so their
    expected counts do not depend on fixture-XML parsing.
    """
    runs = [
        # (run_timestamp, [(classname, name, file_path, status)])
        ("2026-05-01T09:00:00", [
            ("auth.test_oauth", "test_oauth_login", "src/auth/test_oauth.py", "failed"),
            ("auth.test_oauth", "test_logout", "src/auth/test_oauth.py", "passed"),
        ]),
        ("2026-05-10T09:00:00", [
            ("auth.test_oauth", "test_oauth_login", "src/auth/test_oauth.py", "failed"),
            ("auth.test_oauth", "test_logout", "src/auth/test_oauth.py", "passed"),
        ]),
        ("2026-05-18T09:00:00", [
            ("auth.test_oauth", "test_oauth_login", "src/auth/test_oauth.py", "passed"),
            ("auth.test_oauth", "test_logout", "src/auth/test_oauth.py", "passed"),
            ("billing.test_pay", "test_billing_charge", "src/billing/test_pay.py", "failed"),
        ]),
    ]
    for run_ts, cases in runs:
        failures = sum(1 for *_, status in cases if status in domain.FAIL_STATUSES)
        cur = db.execute(
            "INSERT INTO test_runs (suite_name, run_timestamp, total, failures, "
            "errors, skipped, source_file) VALUES (?, ?, ?, ?, 0, 0, ?)",
            ("seed-suite", run_ts, len(cases), failures, f"seed::{run_ts}"),
        )
        run_id = cur.lastrowid
        for classname, name, file_path, status in cases:
            msg = "boom" if status in domain.FAIL_STATUSES else None
            trace = ("Traceback...\n" + "x" * 1200) if status == "failed" else None
            db.execute(
                "INSERT INTO test_cases (run_id, classname, name, file_path, "
                "status, failure_message, failure_type, stack_trace) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, classname, name, file_path, status, msg,
                 "AssertionError" if status == "failed" else None, trace),
            )
    db.commit()
    return db
