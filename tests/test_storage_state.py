"""state.db creation, migration ladder, permissions (plan Appendix A)."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from hermes_flaky_stabilization import paths
from hermes_flaky_stabilization.storage import state

EXPECTED_TABLES = {
    "flaky_verdicts",
    "scan_runs",
    "triage_patterns",
    "healer_runs",
    "recipes",
    "audit",
    "incidents",
    "links",
    "meta",
    "pipeline_runs",
    "schema_version",
}


def _fts5_compiled() -> bool:
    with closing(sqlite3.connect(":memory:")) as conn:
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            return True
        except sqlite3.OperationalError:
            return False


def _table_names(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_fresh_db_reaches_schema_version(profile_env):
    with closing(state.connect()) as conn:
        assert state.current_version(conn) == state.SCHEMA_VERSION
        assert EXPECTED_TABLES <= _table_names(conn)


def test_reopen_is_idempotent(profile_env):
    with closing(state.connect()) as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES ('probe', 'x')")
        conn.commit()
    with closing(state.connect()) as conn:
        assert state.current_version(conn) == state.SCHEMA_VERSION
        row = conn.execute("SELECT value FROM meta WHERE key='probe'").fetchone()
        assert row["value"] == "x"
        # exactly one ladder row per version — the step did not re-run
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert count == state.SCHEMA_VERSION


def test_fts_tables_when_available(profile_env):
    with closing(state.connect()) as conn:
        if _fts5_compiled():
            assert state.fts_available(conn)
            assert {"triage_patterns_fts", "incidents_fts"} <= _table_names(conn)
        else:
            assert not state.fts_available(conn)


def test_wal_mode(profile_env):
    with closing(state.connect()) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"


def test_permissions(profile_env):
    db_path = state.state_db_path()
    with closing(state.connect()):
        pass
    dir_mode = stat.S_IMODE(db_path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(db_path.stat().st_mode)
    assert dir_mode == 0o700, f"data dir mode {oct(dir_mode)}"
    assert file_mode == 0o600, f"db file mode {oct(file_mode)}"


def test_db_lives_under_hermes_home(profile_env):
    assert state.state_db_path().parent == profile_env / "flaky-stabilization"
    assert paths.get_data_dir() == profile_env / "flaky-stabilization"


def test_hermes_home_env_value_is_expanded(monkeypatch, tmp_path):
    """HERMES_HOME often arrives unexpanded (systemd EnvironmentFile, crontab —
    no shell): a literal `~/...` must expand to the user's home, never become
    a relative Path("~/...") that get_data_dir() mkdirs as ./~ under the CWD."""
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("HERMES_HOME", "~/hermes-data")
    resolved = paths.get_hermes_home()
    assert resolved.is_absolute()
    assert resolved == tmp_path / "userhome" / "hermes-data"

    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("HERMES_HOME", "$DATA_ROOT/hermes")
    assert paths.get_hermes_home() == tmp_path / "root" / "hermes"


def _every_stage_resolver() -> dict[str, str]:
    """Every module-level "where is Hermes home?" answer, keyed by owner."""
    from hermes_flaky_stabilization import triage as triage_pkg
    from hermes_flaky_stabilization.detective import storage as detective_storage
    from hermes_flaky_stabilization.history import storage as history_storage
    from hermes_flaky_stabilization.incidents import config as incidents_config
    from hermes_flaky_stabilization.triage import handlers as triage_handlers

    return {
        "paths": str(paths.get_hermes_home()),
        "history.storage": str(history_storage.get_hermes_home()),
        "detective.storage": str(detective_storage.get_hermes_home()),
        "incidents.config": str(incidents_config.resolve_hermes_home()),
        "triage": str(triage_pkg._resolve_home()),
        "triage.handlers": str(triage_handlers._resolve_hermes_home(None)),
    }


@pytest.mark.parametrize(
    ("env_value", "expected_parts"),
    [
        ("~/hermes-data", ("userhome", "hermes-data")),
        ("$DATA_ROOT/hermes", ("root", "hermes")),
    ],
)
def test_all_stage_resolvers_agree_on_hermes_home(
    monkeypatch, tmp_path, env_value, expected_parts
):
    """Every stage must resolve the SAME home from the same env value.

    Five modules used to answer this question with five different lookup
    chains; the three that skipped ``expanduser``/``expandvars`` put history.db
    and the incident index under a literal ``./~`` directory while state.db went
    to the real home — the plugin silently split its data across two roots.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("HERMES_HOME", env_value)

    expected = tmp_path.joinpath(*expected_parts).resolve()
    resolved = _every_stage_resolver()

    for owner, value in resolved.items():
        assert Path(value).is_absolute(), f"{owner} returned a relative path: {value!r}"
        assert Path(value).resolve() == expected, (
            f"{owner} resolved to {value!r}, expected {str(expected)!r}"
        )
    assert len({Path(v).resolve() for v in resolved.values()}) == 1


def test_appendix_a_legacy_column_sets(profile_env):
    """Migration is a plain INSERT..SELECT only if legacy column sets survive
    verbatim — pin the ones the Phase-7 importer relies on."""
    with closing(state.connect()) as conn:
        def cols(table):
            return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

        assert cols("recipes") == [
            "signature", "created_ts", "diagnosis_json", "strategy", "patch_ops_json",
            "hits", "successes", "failures", "last_used_ts", "relaxed_key",
        ]
        assert cols("triage_patterns") == [
            "project", "signature", "category", "occurrences", "first_seen", "last_seen", "sample",
        ]
        assert cols("incidents") == [
            "key", "summary", "status", "root_cause", "reporter", "assignee",
            "created", "updated", "body", "raw_json", "indexed_at",
        ]
        assert cols("flaky_verdicts") == [
            "test_key", "classname", "name", "file_path", "passes", "fails", "runs",
            "window_days", "first_seen", "last_seen", "last_failure", "status", "computed_at",
        ]


def test_future_version_short_circuits(profile_env):
    """A DB stamped at a future version is left alone (no downgrade attempts)."""
    with closing(state.connect()) as conn:
        conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (99)")
        conn.commit()
    with closing(state.connect()) as conn:
        assert state.current_version(conn) == 99


@pytest.mark.parametrize("explicit", [True, False])
def test_connect_with_explicit_path(profile_env, tmp_path, explicit):
    target = tmp_path / "elsewhere" / "state.db" if explicit else None
    with closing(state.connect(target)) as conn:
        assert state.current_version(conn) == state.SCHEMA_VERSION
