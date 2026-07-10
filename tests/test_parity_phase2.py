"""Contract-parity: unified handlers vs the legacy plugins (plan Phase 2 task 3).

For an identical seeded ``history.db``, the unified ``test_failure_lookup`` /
``module_failure_history`` / ``is_flaky`` handlers must return JSON deep-equal
to the legacy handlers' outputs. The legacy repos are imported directly from
their sibling checkouts; the whole module skips cleanly when they are absent so
CI without siblings still runs everything else.

Masked-by-necessity fields (each with a reason, nothing else is masked):
  * ``computed_at``   — stamped by SQLite CURRENT_TIMESTAMP at each scan's own
                        insert time; the two scans run milliseconds apart.
  * ``window_start``  — derived from ``now()`` at call time when ``since`` is
                        defaulted; parity on the explicit-``since`` calls covers
                        the real computation.
  * the ``flaky-detective`` → ``flaky-stab`` CLI rename inside remediation /
    note strings (a documented, plan-mandated rename — Appendix C).
  * ``last_failure_at`` separator — the unified handler normalizes the MAX()
    aggregate through SQLite ``datetime()`` (review fix: raw string MAX
    mis-orders mixed ``T``/space forms), so it emits ``YYYY-MM-DD HH:MM:SS``
    where legacy string-MAXes the raw ``T`` form; only the separator is
    masked, the instant itself is still asserted equal.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SIBLINGS = Path(__file__).resolve().parent.parent.parent
LEGACY_TH = SIBLINGS / "hermes-test-history"
LEGACY_FD = SIBLINGS / "hermes-flaky-detective"

pytestmark = pytest.mark.skipif(
    not (LEGACY_TH.is_dir() and LEGACY_FD.is_dir()),
    reason="legacy sibling checkouts not available",
)


def _import_legacy(dirname: str):
    if str(SIBLINGS) not in sys.path:
        sys.path.insert(0, str(SIBLINGS))
    pkg = importlib.import_module(dirname)
    return pkg


@pytest.fixture
def legacy_th():
    pkg = _import_legacy("hermes-test-history")
    storage = importlib.import_module("hermes-test-history.storage")
    storage.reset_for_tests()
    yield pkg
    storage.reset_for_tests()


@pytest.fixture
def legacy_fd():
    return _import_legacy("hermes-flaky-detective")


@pytest.fixture
def unified_th():
    from hermes_flaky_stabilization import history

    history.storage.reset_for_tests()
    yield history
    history.storage.reset_for_tests()


def _seed_history(conn, now: datetime) -> None:
    """Recent runs: kw_checkout flaky (3 fails + 1 pass), kw_pay always failing."""
    day = timedelta(days=1)
    runs = [
        (now - 6 * day, [("shop.cart", "kw_checkout", "src/shop/test_cart.py", "failed"),
                         ("shop.pay", "kw_pay", "src/shop/test_pay.py", "failed")]),
        (now - 4 * day, [("shop.cart", "kw_checkout", "src/shop/test_cart.py", "failed"),
                         ("shop.pay", "kw_pay", "src/shop/test_pay.py", "failed")]),
        (now - 3 * day, [("shop.cart", "kw_checkout", "src/shop/test_cart.py", "passed"),
                         ("shop.pay", "kw_pay", "src/shop/test_pay.py", "failed")]),
        (now - 2 * day, [("shop.cart", "kw_checkout", "src/shop/test_cart.py", "failed"),
                         ("shop.pay", "kw_pay", "src/shop/test_pay.py", "failed")]),
    ]
    for ts, cases in runs:
        stamp = ts.strftime("%Y-%m-%dT%H:%M:%S")
        failures = sum(1 for *_, s in cases if s == "failed")
        cur = conn.execute(
            "INSERT INTO test_runs (suite_name, run_timestamp, total, failures, errors,"
            " skipped, source_file) VALUES (?, ?, ?, ?, 0, 0, ?)",
            ("parity-suite", stamp, len(cases), failures, f"parity::{stamp}"),
        )
        run_id = cur.lastrowid
        for classname, name, file_path, status in cases:
            conn.execute(
                "INSERT INTO test_cases (run_id, classname, name, file_path, status,"
                " failure_message, failure_type, stack_trace) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, classname, name, file_path, status,
                 "boom" if status == "failed" else None,
                 "AssertionError" if status == "failed" else None,
                 "Trace...\nline2" if status == "failed" else None),
            )
    conn.commit()


def _mask(payload: dict, fields: tuple[str, ...]) -> dict:
    out = json.loads(json.dumps(payload))
    for f in fields:
        out.pop(f, None)
    return out


def _normalize_last_failure_at(payload: dict) -> dict:
    """Mask only the ``T``/space separator in ``top_offenders[].last_failure_at``
    (see the module docstring); the timestamp value stays asserted."""
    out = json.loads(json.dumps(payload))
    for offender in out.get("top_offenders", []):
        stamp = offender.get("last_failure_at")
        if isinstance(stamp, str):
            offender["last_failure_at"] = stamp.replace("T", " ")
    return out


@pytest.fixture
def seeded_home(profile_env, legacy_th, unified_th):
    """One seeded history.db in the shared tmp HERMES_HOME (both worlds read it)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    conn = unified_th.storage.get_connection()
    _seed_history(conn, now)
    return now


# --- history tools ---------------------------------------------------------------


@pytest.mark.parametrize("params", [
    {"test_id": "kw_checkout"},
    {"test_id": "shop.cart::kw_checkout"},
    {"test_id": "src/shop/test_cart.py::kw_checkout", "limit": 2},
    {"test_id": "kw_checkout AND boom"},          # FTS query path
    {"test_id": "no_such_test"},
    {"test_id": ""},                              # validation error envelope
    {"test_id": "x", "limit": 999},               # limit clamp/validation
])
def test_test_failure_lookup_parity(seeded_home, legacy_th, unified_th, params):
    legacy = json.loads(legacy_th._handle_test_failure_lookup(dict(params)))
    unified_pkg = importlib.import_module("hermes_flaky_stabilization.history")
    unified = json.loads(unified_pkg._handle_test_failure_lookup(dict(params)))
    assert unified == legacy


@pytest.mark.parametrize("params", [
    {"path": "src/shop/"},
    {"path": "src/shop/", "min_failures": 4},
    {"path": "src/nowhere/"},
    {"path": "../evil"},                          # rejected traversal
])
def test_module_failure_history_parity_explicit_since(seeded_home, legacy_th,
                                                      unified_th, params):
    since = (seeded_home - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    call = {**params, "since": since}
    legacy = json.loads(legacy_th._handle_module_failure_history(dict(call)))
    unified_pkg = importlib.import_module("hermes_flaky_stabilization.history")
    unified = json.loads(unified_pkg._handle_module_failure_history(dict(call)))
    assert _normalize_last_failure_at(unified) == _normalize_last_failure_at(legacy)


def test_module_failure_history_parity_default_since(seeded_home, legacy_th, unified_th):
    call = {"path": "src/shop/"}
    legacy = json.loads(legacy_th._handle_module_failure_history(dict(call)))
    unified_pkg = importlib.import_module("hermes_flaky_stabilization.history")
    unified = json.loads(unified_pkg._handle_module_failure_history(dict(call)))
    assert _mask(_normalize_last_failure_at(unified), ("window_start",)) == _mask(
        _normalize_last_failure_at(legacy), ("window_start",)
    )


# --- is_flaky --------------------------------------------------------------------


_RENAME = ("hermes flaky-detective scan", "hermes flaky-stab scan")


def _mask_verdict(payload: dict, *, legacy: bool) -> dict:
    out = _mask(payload, ("computed_at",))
    for key in ("note", "remediation", "error"):
        if legacy and isinstance(out.get(key), str):
            out[key] = out[key].replace(*_RENAME).replace(
                "that hermes-test-history has ingested results",
                "that test history has ingested results",
            )
    return out


@pytest.mark.parametrize("test_id", [
    "kw_checkout",                # flaky
    "kw_pay",                     # consistently failing
    "shop.cart::kw_checkout",     # exact key
    "missing_test",               # unknown verdict
    "",                           # validation error
])
def test_is_flaky_parity(seeded_home, legacy_fd, unified_th, test_id):
    from hermes_flaky_stabilization import detective as unified_fd

    legacy_storage = importlib.import_module("hermes-flaky-detective.storage")
    legacy_scan = importlib.import_module("hermes-flaky-detective.scan")
    from hermes_flaky_stabilization.detective import scan as unified_scan
    from hermes_flaky_stabilization.detective import storage as unified_storage

    db_path = unified_th.storage.get_db_path()
    now = datetime.now(UTC)
    with legacy_storage.Storage() as store:
        legacy_scan.run_scan(store, window_days=14, min_fails=3, include_errors=True,
                             db_path=db_path, now=now)
        legacy = json.loads(legacy_fd._handle_is_flaky({"test_id": test_id}, store=store))
    with unified_storage.Storage() as store:
        unified_scan.run_scan(store, window_days=14, min_fails=3, include_errors=True,
                              db_path=db_path, now=now)
        unified = json.loads(unified_fd._handle_is_flaky({"test_id": test_id}, store=store))

    assert _mask_verdict(unified, legacy=False) == _mask_verdict(legacy, legacy=True)
