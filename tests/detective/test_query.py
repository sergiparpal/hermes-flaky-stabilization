"""Tests for the read-only test-history reader (``query.py``) and path resolution.

Exercised against a synthetic test-history DB (``fixtures.build_test_history_db``)
so the assertions do not depend on a live test-history install or XML parsing.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fixtures import build_test_history_db
from hermes_flaky_detective import config, query, timeutil

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
CUTOFF = timeutil.window_cutoff(NOW, 14)   # 2026-05-18T12:00:00


def _read(path):
    return query.read_test_history(path, CUTOFF)


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


def test_windowing_by_effective_timestamp(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "in.xml", "run_timestamp": "2026-05-20T09:00:00",
         "cases": [{"name": "test_in", "status": "failed"}]},
        {"source_file": "out.xml", "run_timestamp": "2026-05-01T09:00:00",  # before cutoff
         "cases": [{"name": "test_out", "status": "failed"}]},
    ])
    names = {r[1] for r in _read(db).rows}
    assert names == {"test_in"}


def test_null_run_timestamp_falls_back_to_ingested_at(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        # No run_timestamp -> windowing uses ingested_at (inside window).
        {"source_file": "a.xml", "run_timestamp": None, "ingested_at": "2026-05-25 09:00:00",
         "cases": [{"name": "test_recent", "status": "failed"}]},
        # No run_timestamp, ingested_at before cutoff -> excluded.
        {"source_file": "b.xml", "run_timestamp": None, "ingested_at": "2026-05-01 09:00:00",
         "cases": [{"name": "test_old", "status": "failed"}]},
    ])
    rows = _read(db).rows
    names = {r[1] for r in rows}
    assert names == {"test_recent"}
    # eff_ts should be the (normalized) ingested_at for the surviving row.
    eff = {r[1]: r[4] for r in rows}
    assert eff["test_recent"] == "2026-05-25T09:00:00"


# ---------------------------------------------------------------------------
# Reingest dedup by (source_file, run_timestamp)
# ---------------------------------------------------------------------------


def test_dedup_keeps_latest_ingest_of_same_logical_run(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "ci/junit.xml", "run_timestamp": "2026-05-20T09:00:00",
         "ingested_at": "2026-05-20 09:05:00",
         "cases": [{"name": "test_x", "status": "failed"}]},
        # Same (source_file, run_timestamp), re-ingested later with a different outcome.
        {"source_file": "ci/junit.xml", "run_timestamp": "2026-05-20T09:00:00",
         "ingested_at": "2026-05-21 10:00:00",
         "cases": [{"name": "test_x", "status": "passed"}]},
    ])
    rows = _read(db).rows
    assert len(rows) == 1                      # the duplicate logical run is collapsed
    assert rows[0][1] == "test_x"
    assert rows[0][3] == "passed"              # the most-recently-ingested wins


def test_distinct_run_timestamps_are_not_deduped(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "ci/junit.xml", "run_timestamp": "2026-05-20T09:00:00",
         "cases": [{"name": "test_x", "status": "failed"}]},
        {"source_file": "ci/junit.xml", "run_timestamp": "2026-05-21T09:00:00",
         "cases": [{"name": "test_x", "status": "passed"}]},
    ])
    rows = _read(db).rows
    assert len(rows) == 2                       # different timestamps = two logical runs


# ---------------------------------------------------------------------------
# skipped exclusion / row shape / jest-style nulls
# ---------------------------------------------------------------------------


def test_skipped_cases_excluded(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "a.xml", "run_timestamp": "2026-05-20T09:00:00", "cases": [
            {"name": "test_run", "status": "failed"},
            {"name": "test_skipped", "status": "skipped"},
        ]},
    ])
    names = {r[1] for r in _read(db).rows}
    assert names == {"test_run"}


def test_row_shape_and_canonical_eff_ts(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "a.xml", "run_timestamp": "2026-05-20T09:00:00", "cases": [
            {"classname": "pkg.Mod", "name": "test_a", "file_path": "src/mod.py",
             "status": "failed"},
        ]},
    ])
    rows = _read(db).rows
    assert len(rows) == 1
    classname, name, file_path, status, eff_ts = rows[0]
    assert (classname, name, file_path, status) == ("pkg.Mod", "test_a", "src/mod.py", "failed")
    assert eff_ts == "2026-05-20T09:00:00"     # canonical naive-UTC ISO seconds


def test_jest_style_null_file_path_preserved(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "a.xml", "run_timestamp": "2026-05-20T09:00:00", "cases": [
            {"classname": None, "name": "renders header", "file_path": None, "status": "failed"},
        ]},
    ])
    rows = _read(db).rows
    assert rows[0][0] is None        # classname
    assert rows[0][2] is None        # file_path


# ---------------------------------------------------------------------------
# Fallback path parity (no window functions)
# ---------------------------------------------------------------------------


def test_fallback_matches_window_function_path(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "ci/junit.xml", "run_timestamp": "2026-05-20T09:00:00",
         "ingested_at": "2026-05-20 09:05:00",
         "cases": [{"name": "test_x", "status": "failed"},
                   {"name": "test_y", "status": "skipped"}]},
        {"source_file": "ci/junit.xml", "run_timestamp": "2026-05-20T09:00:00",
         "ingested_at": "2026-05-21 10:00:00",
         "cases": [{"name": "test_x", "status": "passed"}]},
        {"source_file": "other.xml", "run_timestamp": "2026-05-22T09:00:00",
         "cases": [{"name": "test_z", "status": "error"}]},
    ])
    conn = sqlite3.connect(query.read_only_uri(db), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        primary = query._read_windowed(conn, CUTOFF)
        fallback = query._fallback_read(conn, CUTOFF)
    finally:
        conn.close()
    # Both paths agree on *order*, not just contents — each applies a deterministic
    # ORDER BY (eff_ts, then case insertion order), so the core sees identical input.
    assert primary == fallback


def test_fallback_read_tolerates_numeric_run_timestamp(tmp_path):
    # A numeric run_timestamp (here a julian-day float, which SQLite's datetime()
    # accepts, so the row survives the window filter) used to crash the fallback
    # path: it sorted the *raw* rows, comparing float to str -> TypeError. It must
    # normalize before sorting, like the window-function path. Row *contents* must
    # agree between the paths (order may differ for this pathological input: SQL
    # sorts numerics before text, normalized strings sort lexically).
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "a.xml", "run_timestamp": 2461188.5,   # 2026-05-28 (julian day)
         "cases": [{"name": "t_num", "status": "failed"}]},
        {"source_file": "b.xml", "run_timestamp": "2026-05-20T09:00:00",
         "cases": [{"name": "t_str", "status": "failed"}]},
    ])
    conn = sqlite3.connect(query.read_only_uri(db), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        primary = query._read_windowed(conn, CUTOFF)
        fallback = query._fallback_read(conn, CUTOFF)
    finally:
        conn.close()
    assert set(fallback) == set(primary)
    assert all(isinstance(r[4], str) for r in fallback)   # normalized, core-safe


def test_window_path_orders_rows_by_effective_time(tmp_path):
    # Runs inserted out of chronological order must come back ordered by eff_ts, so
    # the core's output order (and file_path backfill) is deterministic, not join-order.
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "c.xml", "run_timestamp": "2026-05-24T09:00:00",
         "cases": [{"name": "t3", "status": "failed"}]},
        {"source_file": "a.xml", "run_timestamp": "2026-05-20T09:00:00",
         "cases": [{"name": "t1", "status": "failed"}]},
        {"source_file": "b.xml", "run_timestamp": "2026-05-22T09:00:00",
         "cases": [{"name": "t2", "status": "failed"}]},
    ])
    eff = [r[4] for r in _read(db).rows]
    assert eff == sorted(eff)


# ---------------------------------------------------------------------------
# Reingest dedup needs a run_timestamp — warn when it cannot be applied
# ---------------------------------------------------------------------------


def test_warns_when_timestampless_runs_cannot_be_deduped(tmp_path, caplog):
    # Same source_file ingested twice with NO run_timestamp: the dedup key falls back
    # to ingested_at (unique per ingest), so the two ingests cannot be collapsed and
    # test_x is counted twice. The reader must warn so this is not silent.
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "ci/junit.xml", "run_timestamp": None, "ingested_at": "2026-05-20 09:05:00",
         "cases": [{"name": "test_x", "status": "failed"}]},
        {"source_file": "ci/junit.xml", "run_timestamp": None, "ingested_at": "2026-05-20 10:00:00",
         "cases": [{"name": "test_x", "status": "failed"}]},
    ])
    with caplog.at_level("WARNING"):
        rows = _read(db).rows
    assert len(rows) == 2                                   # not deduped (the inherent limit)
    assert any("run_timestamp" in rec.message for rec in caplog.records)


def test_no_undedupable_warning_when_runs_have_timestamps(tmp_path, caplog):
    # A single timestamp-less run, and distinct timestamped runs, must NOT warn.
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "a.xml", "run_timestamp": None, "ingested_at": "2026-05-20 09:00:00",
         "cases": [{"name": "t1", "status": "failed"}]},
        {"source_file": "b.xml", "run_timestamp": "2026-05-21T09:00:00",
         "cases": [{"name": "t2", "status": "failed"}]},
        {"source_file": "b.xml", "run_timestamp": "2026-05-22T09:00:00",
         "cases": [{"name": "t2", "status": "failed"}]},
    ])
    with caplog.at_level("WARNING"):
        _read(db)
    assert not any("run_timestamp" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Row normalization is type-safe (a non-string eff_ts must not crash the core)
# ---------------------------------------------------------------------------


def test_normalize_rows_coerces_non_string_eff_ts(tmp_path):
    from hermes_flaky_detective import detect

    # A numeric/epoch eff_ts mixed with a string one previously made the core compare
    # str vs int -> TypeError. Normalization must yield only strings (or None).
    raw = [("c", "n", "f", "passed", 1716192000),
           ("c", "n", "f", "failed", "2026-05-20T09:00:00")]
    normalized = query._normalize_rows(raw)
    assert all(isinstance(r[4], str) for r in normalized)
    # And the pure core consumes the result without raising.
    verdicts = detect.compute_verdicts(normalized, None, 14, 1, True)
    assert verdicts[0].runs == 2


# ---------------------------------------------------------------------------
# schema_version guard
# ---------------------------------------------------------------------------


def test_reads_source_schema_version(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "a.xml", "run_timestamp": "2026-05-20T09:00:00",
         "cases": [{"name": "t", "status": "failed"}]},
    ], schema_version=1)
    assert _read(db).source_schema_version == 1


def test_future_schema_is_refused(tmp_path, caplog):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "a.xml", "run_timestamp": "2026-05-20T09:00:00",
         "cases": [{"name": "t", "status": "failed"}]},
    ], schema_version=999)
    with pytest.raises(query.TestHistoryUnavailable, match="newer than supported"):
        _read(db)


# ---------------------------------------------------------------------------
# Read-only enforcement / error handling
# ---------------------------------------------------------------------------


def test_read_only_uri_contains_mode_ro(tmp_path):
    assert query.read_only_uri(tmp_path / "h.db").endswith("?mode=ro")


def test_read_only_uri_percent_encodes_special_chars(tmp_path):
    # A raw `#`, `?`, or `%` in the DB path used to terminate/alter the URI's path
    # part, silently dropping mode=ro and opening (even creating) a *different*
    # file read-write. The path must be percent-encoded — SQLite decodes escapes
    # in URI filenames — so the URI resolves to the same file, still read-only.
    db = build_test_history_db(tmp_path / "hi#st%y.db", [
        {"source_file": "a.xml", "run_timestamp": "2026-05-20T09:00:00",
         "cases": [{"name": "t", "status": "failed"}]},
    ])
    before = {p.name for p in tmp_path.iterdir()}
    conn = sqlite3.connect(query.read_only_uri(db), uri=True)
    try:
        # Resolves to the *same* file (its data is readable) ...
        assert conn.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0] == 1
        # ... and is genuinely read-only (a write must fail).
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE pwned (x INTEGER)")
    finally:
        conn.close()
    assert {p.name for p in tmp_path.iterdir()} == before   # no stray file created


def test_connection_is_read_only(tmp_path):
    db = build_test_history_db(tmp_path / "h.db", [
        {"source_file": "a.xml", "run_timestamp": "2026-05-20T09:00:00",
         "cases": [{"name": "t", "status": "failed"}]},
    ])
    conn = sqlite3.connect(query.read_only_uri(db), uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO test_runs (suite_name, source_file) VALUES ('x', 'y')")
            conn.commit()
    finally:
        conn.close()


def test_missing_db_raises_clean_error(tmp_path):
    with pytest.raises(query.TestHistoryUnavailable) as exc:
        _read(tmp_path / "does-not-exist.db")
    assert "not found" in str(exc.value)


def test_non_test_history_file_raises_clean_error(tmp_path):
    bogus = tmp_path / "bogus.db"
    conn = sqlite3.connect(str(bogus))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(query.TestHistoryUnavailable):
        _read(bogus)


def test_corrupt_db_raises_clean_error(tmp_path):
    # A genuinely corrupt / non-SQLite file raises sqlite3.DatabaseError ("file is
    # not a database"), which is NOT an OperationalError. The reader must still
    # surface a clean TestHistoryUnavailable, never an uncaught traceback.
    bogus = tmp_path / "corrupt.db"
    bogus.write_bytes(b"this is not a sqlite database at all\n" * 8)
    with pytest.raises(query.TestHistoryUnavailable):
        _read(bogus)


# ---------------------------------------------------------------------------
# test-history DB path resolution (§5.6)
# ---------------------------------------------------------------------------


def test_resolve_db_path_default(profile_env):
    cfg = config.get_config()
    path = config.resolve_test_history_db_path(cfg)
    assert path == profile_env / "test-history" / "history.db"


def test_resolve_db_path_override(profile_env, tmp_path):
    override = tmp_path / "custom" / "th.db"
    path = config.resolve_test_history_db_path({"test_history_db_path": str(override)})
    assert path == Path(override).resolve()


def test_resolve_db_path_expands_tilde():
    # A "~/..." override must expand to $HOME, not a literal "~" dir under cwd.
    path = config.resolve_test_history_db_path({"test_history_db_path": "~/myhist/history.db"})
    text = str(path).replace("\\", "/")
    assert "~" not in text
    assert text.endswith("myhist/history.db")


# ---------------------------------------------------------------------------
# config value coercion (a hand-edited config must never crash the scan path)
# ---------------------------------------------------------------------------


def test_get_config_coerces_malformed_values(profile_env):
    # An unparseable int -> the default; a stringified int -> int; the string
    # "false" -> False (a typo, never bool("false") which is True).
    config.config_path().write_text(
        '{"detective": {"min_fails": "oops", "window_days": "7", '
        '"include_errors": "false"}}',
        encoding="utf-8",
    )
    cfg = config.get_config()
    assert cfg["min_fails"] == config.DEFAULT_CONFIG["min_fails"]
    assert cfg["window_days"] == 7 and isinstance(cfg["window_days"], int)
    assert cfg["include_errors"] is False
