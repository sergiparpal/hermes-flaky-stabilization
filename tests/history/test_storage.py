"""Connection/config storage behavior: WAL mode, db_path_override, permissions."""

import json
import os
import stat

import pytest
from hermes_test_history import storage


def test_connection_uses_wal_journal_mode(db):
    # WAL lets read-only gateway tool queries run concurrently with a CLI
    # ingest/prune writer in another process, instead of blocking on the
    # rollback-journal lock.
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_connection_like_is_case_sensitive(db):
    # case_sensitive_like is ON so module_failure_history's file_path LIKE
    # 'prefix%' can use idx_cases_module as a range seek instead of a full scan.
    # The pragma has no query form, so assert the resulting behavior directly.
    assert db.execute("SELECT 'ABC' LIKE 'abc'").fetchone()[0] == 0
    assert db.execute("SELECT 'abc' LIKE 'abc'").fetchone()[0] == 1


def test_db_path_override_creates_missing_parent(profile_env):
    # An override pointing into a not-yet-existing directory (within the Hermes
    # home) must work: get_db_path creates the parent, mirroring get_storage_dir()
    # for the default path.
    target = profile_env / "nested" / "deep" / "history.db"   # within HERMES_HOME
    config_path = storage.get_storage_dir() / "config.json"
    config_path.write_text(json.dumps({"db_path_override": str(target)}), encoding="utf-8")
    storage.reset_for_tests()  # drop the cached config so the override is read

    conn = storage.get_connection()  # must not raise "unable to open database file"
    assert target.exists()
    assert storage.get_db_path().resolve() == target.resolve()
    # Sanity: the schema was applied at the override location.
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='test_cases'"
    ).fetchone()[0] == 1


def test_db_path_override_outside_home_is_rejected(profile_env, tmp_path):
    # A config that points the DB outside the Hermes home must be refused so a
    # stray (or attacker-written) config cannot redirect the DB — and the mkdir
    # it triggers — to an arbitrary filesystem location. tmp_path is the parent
    # of HERMES_HOME, so a sibling of it is outside the home.
    outside = tmp_path / "evil" / "history.db"
    config_path = storage.get_storage_dir() / "config.json"
    config_path.write_text(json.dumps({"db_path_override": str(outside)}), encoding="utf-8")
    storage.reset_for_tests()
    with pytest.raises(ValueError):
        storage.get_db_path()
    assert not outside.parent.exists()  # rejected before any mkdir


def test_db_and_storage_dir_are_owner_only(db):
    # The DB holds captured test output (stack traces / failure messages) that can
    # carry sensitive data; it must not be world-readable on a shared host.
    dir_mode = stat.S_IMODE(os.stat(storage.get_storage_dir()).st_mode)
    db_mode = stat.S_IMODE(os.stat(storage.get_db_path()).st_mode)
    assert dir_mode == 0o700, oct(dir_mode)
    assert db_mode == 0o600, oct(db_mode)
