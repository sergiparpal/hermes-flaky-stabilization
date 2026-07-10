"""Tests for the verdicts-DB lifecycle and snapshot writes (``storage.py``).

Everything runs against a temp HERMES_HOME (the ``profile_env`` fixture), so no
real Hermes home is touched.
"""

import os
import stat

from hermes_flaky_detective import detect, domain, storage


def _v(test_key, status, *, passes=0, fails=0, name=None, classname="pkg.Mod",
       file_path="src/mod.py", window_days=14):
    name = name or test_key.split("::")[-1]
    return detect.Verdict(
        test_key=test_key, classname=classname, name=name, file_path=file_path,
        passes=passes, fails=fails, runs=passes + fails, window_days=window_days,
        first_seen="2026-05-20T09:00:00", last_seen="2026-05-25T09:00:00",
        last_failure="2026-05-24T09:00:00" if fails else None, status=status,
    )


def test_state_schema_creates_verdict_tables(verdicts_db):
    # Pins the real DDL path: opening the storage connection runs the unified
    # state.db schema (storage/state.py), which must create the verdict tables.
    names = {r["name"] for r in verdicts_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"flaky_verdicts", "scan_runs", "schema_version"} <= names


def test_replace_verdicts_is_a_full_snapshot_replace(verdicts_db):
    storage.replace_verdicts(verdicts_db, [
        _v("a::t1", domain.VERDICT_FLAKY, passes=2, fails=3),
        _v("b::t2", domain.VERDICT_STABLE, passes=5, fails=0),
    ])
    assert {r["test_key"] for r in verdicts_db.execute("SELECT test_key FROM flaky_verdicts")} \
        == {"a::t1", "b::t2"}

    # Second write must fully replace the first (the old keys disappear).
    storage.replace_verdicts(verdicts_db, [
        _v("c::t3", domain.VERDICT_CONSISTENTLY_FAILING, passes=0, fails=4),
    ])
    rows = verdicts_db.execute("SELECT test_key, status FROM flaky_verdicts").fetchall()
    assert len(rows) == 1
    assert rows[0]["test_key"] == "c::t3"
    assert rows[0]["status"] == domain.VERDICT_CONSISTENTLY_FAILING


def test_replace_with_empty_clears_table(verdicts_db):
    storage.replace_verdicts(verdicts_db, [_v("a::t1", domain.VERDICT_FLAKY, fails=3, passes=1)])
    storage.replace_verdicts(verdicts_db, [])
    assert verdicts_db.execute("SELECT COUNT(*) FROM flaky_verdicts").fetchone()[0] == 0


def test_scan_runs_appended_and_last_scan(verdicts_db):
    storage.record_scan_run(verdicts_db, window_days=14, min_fails=3, include_errors=True,
                            source_schema_version=1, tests_examined=10, flaky_found=2)
    storage.record_scan_run(verdicts_db, window_days=7, min_fails=2, include_errors=False,
                            source_schema_version=1, tests_examined=20, flaky_found=5)
    assert verdicts_db.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 2
    last = storage.last_scan_run(verdicts_db)
    assert (last["window_days"], last["min_fails"], last["include_errors"]) == (7, 2, 0)
    assert (last["tests_examined"], last["flaky_found"]) == (20, 5)


def test_read_flaky_keys_only_returns_flaky(verdicts_db):
    storage.replace_verdicts(verdicts_db, [
        _v("a::t1", domain.VERDICT_FLAKY, passes=1, fails=3),
        _v("b::t2", domain.VERDICT_CONSISTENTLY_FAILING, passes=0, fails=4),
        _v("c::t3", domain.VERDICT_STABLE, passes=5, fails=0),
    ])
    assert storage.read_flaky_keys(verdicts_db) == {"a::t1"}


def test_get_verdict_and_read_verdicts_and_counts(verdicts_db):
    storage.replace_verdicts(verdicts_db, [
        _v("a::t1", domain.VERDICT_FLAKY, passes=1, fails=5),
        _v("b::t2", domain.VERDICT_FLAKY, passes=2, fails=3),
        _v("c::t3", domain.VERDICT_STABLE, passes=5, fails=0),
    ])
    assert storage.get_verdict(verdicts_db, "a::t1")["fails"] == 5
    assert storage.get_verdict(verdicts_db, "missing::x") is None

    flaky = storage.read_verdicts(verdicts_db, statuses=[domain.VERDICT_FLAKY])
    assert [r["test_key"] for r in flaky] == ["a::t1", "b::t2"]   # worst-first ordering

    assert storage.count_by_status(verdicts_db) == {domain.VERDICT_FLAKY: 2, domain.VERDICT_STABLE: 1}


def test_storage_permissions_are_owner_only(profile_env):
    with storage.Storage() as store:
        store.conn                       # force the lazy open (creates dir + DB file)
        storage_dir = storage.get_storage_dir()
        db_path = storage.get_db_path()
        assert stat.S_IMODE(os.stat(storage_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600


def test_verdict_field_set_has_a_single_owner(verdicts_db):
    # P2 pin: the verdict field set is declared once (the Verdict dataclass) and the
    # INSERT columns and the DDL must both track it. If any of the three drifts,
    # this fails — catching the "add a field, forget one place" shotgun-surgery bug.
    from dataclasses import fields

    dataclass_fields = [f.name for f in fields(detect.Verdict)]
    ddl_cols = [r["name"] for r in verdicts_db.execute("PRAGMA table_info(flaky_verdicts)")
                if r["name"] != "computed_at"]   # DB-only column, defaulted
    assert list(storage._VERDICT_COLS) == dataclass_fields
    assert ddl_cols == dataclass_fields
