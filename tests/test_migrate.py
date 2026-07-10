"""`flaky-stab migrate` — copy-based, idempotent, sources untouched (Phase 7)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from hermes_flaky_stabilization.storage import migrate_legacy, state

# --- legacy fixture builders (raw SQL matching the legacy schemas) ------------------


def _build_verdicts_db(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            CREATE TABLE flaky_verdicts (
                test_key TEXT PRIMARY KEY, classname TEXT, name TEXT NOT NULL,
                file_path TEXT, passes INTEGER NOT NULL, fails INTEGER NOT NULL,
                runs INTEGER NOT NULL, window_days INTEGER NOT NULL,
                first_seen TIMESTAMP, last_seen TIMESTAMP, last_failure TIMESTAMP,
                status TEXT NOT NULL,
                computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                window_days INTEGER NOT NULL, min_fails INTEGER NOT NULL,
                include_errors INTEGER NOT NULL, source_schema_version INTEGER,
                tests_examined INTEGER NOT NULL, flaky_found INTEGER NOT NULL);
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO schema_version (version) VALUES (1);
        """)
        conn.execute(
            "INSERT INTO flaky_verdicts (test_key, classname, name, passes, fails,"
            " runs, window_days, status) VALUES ('a::t1', 'a', 't1', 2, 3, 5, 14,"
            " 'flaky'), ('b::t2', 'b', 't2', 5, 0, 5, 14, 'stable')")
        conn.execute(
            "INSERT INTO scan_runs (window_days, min_fails, include_errors,"
            " source_schema_version, tests_examined, flaky_found)"
            " VALUES (14, 3, 1, 1, 2, 1)")
        conn.commit()
    return {"flaky_verdicts": 2, "scan_runs": 1}


def _build_patterns_db(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            CREATE TABLE patterns (
                project TEXT NOT NULL, signature TEXT NOT NULL, category TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, sample TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project, signature));
        """)
        conn.execute(
            "INSERT INTO patterns VALUES ('proj', 'sig1', 'flaky', 4,"
            " '2026-01-01', '2026-06-01', 'AssertionError boom')")
        conn.commit()
    return {"patterns": 1}


def _build_healer_db(path, *, v1: bool):
    from hermes_flaky_stabilization.healer.flaky_healer.recipes.signature import (
        signature_parts,
    )

    parts = signature_parts(error_class="E", failing_action="click",
                            selector="#a", message="m")
    blob = json.dumps({"diagnosis": {"cause": "x"}, "signature_parts": parts})
    with closing(sqlite3.connect(path)) as conn:
        relaxed_col = "" if v1 else ", relaxed_key TEXT"
        conn.executescript(f"""
            CREATE TABLE runs (id INTEGER PRIMARY KEY, ts TEXT, source TEXT,
                build_id TEXT, test_id TEXT, diagnosis_json TEXT, strategy TEXT,
                result TEXT, isolation TEXT, duration_s REAL);
            CREATE TABLE recipes (signature TEXT PRIMARY KEY, created_ts TEXT,
                diagnosis_json TEXT, strategy TEXT, patch_ops_json TEXT,
                hits INTEGER DEFAULT 0, successes INTEGER DEFAULT 0,
                failures INTEGER DEFAULT 0, last_used_ts TEXT{relaxed_col});
            CREATE TABLE audit (id INTEGER PRIMARY KEY, ts TEXT, event TEXT,
                payload_json TEXT);
        """)
        conn.execute(f"PRAGMA user_version = {1 if v1 else 2}")
        conn.execute(
            "INSERT INTO runs (ts, source, test_id, diagnosis_json, strategy,"
            " result, isolation, duration_s) VALUES ('2026-01-01', 'tool', 't',"
            " '{}', 'bump_timeout', 'stable', 'docker', 1.5)")
        conn.execute(
            "INSERT INTO recipes (signature, created_ts, diagnosis_json, strategy,"
            " patch_ops_json) VALUES ('legacy-sig', '2026-01-01', ?, 'bump_timeout',"
            " '[]')", (blob,))
        conn.execute(
            "INSERT INTO audit (ts, event, payload_json) VALUES"
            " ('2026-01-01', 'pre_approval_request', '{}')")
        conn.commit()
    return {"runs": 1, "recipes": 1, "audit": 1}, parts


def _build_incidents_db(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            CREATE TABLE incidents (key TEXT PRIMARY KEY, summary TEXT, status TEXT,
                root_cause TEXT, reporter TEXT, assignee TEXT, created TEXT,
                updated TEXT, body TEXT, raw_json TEXT, indexed_at TEXT);
            CREATE TABLE links (id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, incident_key TEXT, note TEXT, created_at TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.execute(
            "INSERT INTO incidents (key, summary, status, root_cause, body)"
            " VALUES ('INC-1', 'db outage', 'Resolved', 'pool leak', 'details'),"
            " ('INC-2', 'checkout broken', 'Open', '', 'safari only')")
        conn.execute(
            "INSERT INTO links (session_id, incident_key, note, created_at)"
            " VALUES ('s1', 'INC-1', 'seen', '2026-01-01')")
        conn.execute("INSERT INTO meta VALUES ('sync_watermark', '2026-06-01')")
        conn.commit()
    return {"incidents": 2, "links": 1, "meta": 1}


@pytest.fixture
def legacy_home(profile_env):
    """A HERMES_HOME populated with all four legacy DBs at their real paths."""
    home = profile_env
    (home / "flaky-detective").mkdir(parents=True)
    (home / "cache").mkdir(parents=True)
    (home / "plugins-data" / "hermes-flaky-healer").mkdir(parents=True)
    counts = {}
    counts["flaky_detective"] = _build_verdicts_db(
        home / "flaky-detective" / "verdicts.db")
    counts["ci_triage"] = _build_patterns_db(home / "cache" / "ci_triage_patterns.db")
    healer_counts, parts = _build_healer_db(
        home / "plugins-data" / "hermes-flaky-healer" / "healer.db", v1=True)
    counts["flaky_healer"] = healer_counts
    counts["jira_incidents"] = _build_incidents_db(home / "hermes-jira-incidents.db")
    return home, counts, parts


def _hashes(home):
    out = {}
    for path in migrate_legacy.default_sources(home).values():
        if path.exists():
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _state_counts():
    with closing(state.connect()) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("flaky_verdicts", "scan_runs", "triage_patterns",
                          "healer_runs", "recipes", "audit", "incidents",
                          "links", "meta")
        }


def test_migrate_copies_everything_idempotently(legacy_home):
    home, legacy_counts, parts = legacy_home
    before_hashes = _hashes(home)

    reports = {r.name: r for r in migrate_legacy.migrate(home)}
    assert reports["flaky_detective"].tables == {"flaky_verdicts": 2, "scan_runs": 1}
    assert reports["ci_triage"].tables == {"triage_patterns": 1}
    assert reports["flaky_healer"].tables == {"healer_runs": 1, "recipes": 1, "audit": 1}
    assert reports["jira_incidents"].tables == {"incidents": 2, "links": 1, "meta": 1}

    counts = _state_counts()
    assert counts["flaky_verdicts"] == 2 and counts["scan_runs"] == 1
    assert counts["triage_patterns"] == 1
    assert counts["healer_runs"] == 1 and counts["recipes"] == 1 and counts["audit"] == 1
    assert counts["incidents"] == 2 and counts["links"] == 1
    # meta = 1 copied watermark + 4 provenance rows
    assert counts["meta"] == 5

    # (a) second run is a no-op ------------------------------------------------
    second = {r.name: r for r in migrate_legacy.migrate(home)}
    assert all(r.skipped and r.skipped.startswith("already migrated")
               for r in second.values())
    assert _state_counts() == counts

    # (c) sources byte-identical before/after ----------------------------------
    assert _hashes(home) == before_hashes


def test_migrate_backfills_v1_recipe_relaxed_key(legacy_home):
    from hermes_flaky_stabilization.healer.flaky_healer.recipes.signature import (
        relaxed_key,
    )

    home, _counts, parts = legacy_home
    # relaxed_key is injected (as the CLI does) — migrate no longer imports it.
    migrate_legacy.migrate(home, relaxed_key_fn=relaxed_key)
    with closing(state.connect()) as conn:
        row = conn.execute(
            "SELECT relaxed_key FROM recipes WHERE signature='legacy-sig'").fetchone()
    assert row["relaxed_key"] == relaxed_key(parts)

    # And the healer store's indexed relaxed matching now finds it.
    from hermes_flaky_stabilization.healer.flaky_healer.recipes.store import HealerStore

    store = HealerStore(state.state_db_path())
    assert [r.signature for r in store.recipes_by_relaxed_key(relaxed_key(parts))] == [
        "legacy-sig"]


def test_migrate_dry_run_writes_nothing(legacy_home):
    home, _counts, _parts = legacy_home
    before_hashes = _hashes(home)
    reports = {r.name: r for r in migrate_legacy.migrate(home, dry_run=True)}
    # The dry run counts through the same legacy->target mapping the copiers
    # use and reports TARGET names — including the sources whose legacy names
    # differ (healer `runs` -> `healer_runs`, triage `patterns` ->
    # `triage_patterns`) — so its output predicts the real run.
    assert reports["jira_incidents"].tables == {"incidents": 2, "links": 1, "meta": 1}
    assert reports["flaky_healer"].tables == {"healer_runs": 1, "recipes": 1, "audit": 1}
    assert reports["ci_triage"].tables == {"triage_patterns": 1}
    assert reports["flaky_detective"].tables == {"flaky_verdicts": 2, "scan_runs": 1}
    assert all(r.skipped == "dry-run (nothing written)" for r in reports.values())
    counts = _state_counts()
    assert all(count == 0 for count in counts.values()), counts
    assert _hashes(home) == before_hashes
    # A real run after the dry run copies exactly what the dry run predicted.
    real = {r.name: r for r in migrate_legacy.migrate(home)}
    assert ({name: r.tables for name, r in real.items()}
            == {name: r.tables for name, r in reports.items()})
    assert _state_counts()["incidents"] == 2


def test_migrate_missing_sources_reported(profile_env):
    reports = {r.name: r for r in migrate_legacy.migrate(profile_env)}
    assert all(not r.found and r.skipped == "source not found"
               for r in reports.values())


def test_attach_ro_encodes_paths_with_uri_metacharacters(profile_env, tmp_path):
    """A legacy DB path containing '?' must still attach read-only.

    `--healer-db` takes an operator-supplied path. _attach_ro used to build
    `file:{path}?mode=ro` by hand: for `/x/db?a=1/healer.db` SQLite parses the
    path as `/x/db` and finds NO `mode` parameter, so it would open a different
    file READ-WRITE. The percent-encoded paths.read_only_uri prevents that.
    """
    weird_dir = tmp_path / "db?a=1"
    weird_dir.mkdir()
    healer_db = weird_dir / "healer.db"
    _build_healer_db(healer_db, v1=False)
    before = hashlib.sha256(healer_db.read_bytes()).hexdigest()

    reports = {r.name: r for r in migrate_legacy.migrate(
        profile_env, overrides={"flaky_healer": healer_db})}

    healer = reports["flaky_healer"]
    assert healer.found, f"source not found via the '?' path: {healer.skipped}"
    assert healer.skipped is None, f"migration skipped: {healer.skipped}"
    # The source must be byte-identical: attached read-only, never checkpointed.
    assert hashlib.sha256(healer_db.read_bytes()).hexdigest() == before


def test_migrate_never_clobbers_newer_unified_rows(legacy_home):
    home, _counts, _parts = legacy_home
    with closing(state.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO incidents (key, summary, status) VALUES"
            " ('INC-1', 'NEWER unified row', 'Closed')")
    migrate_legacy.migrate(home)
    with closing(state.connect()) as conn:
        row = conn.execute("SELECT summary FROM incidents WHERE key='INC-1'").fetchone()
    assert row["summary"] == "NEWER unified row"  # INSERT OR IGNORE kept ours


def test_migrate_keeps_legacy_rows_despite_autoincrement_id_collisions(legacy_home):
    """Surrogate autoincrement ids must NOT be copied: if the unified DB
    already wrote rows (a scan/heal/link before migrating), its rowids start
    at 1 exactly like the legacy DBs', and copying legacy ids via INSERT OR
    IGNORE would silently drop every colliding legacy row — unrecoverably,
    since the provenance marker blocks re-runs. Fresh rowids keep them all."""
    home, _counts, _parts = legacy_home
    with closing(state.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO scan_runs (window_days, min_fails, include_errors,"
            " tests_examined, flaky_found) VALUES (7, 1, 1, 10, 0)")
        conn.execute("INSERT INTO healer_runs (ts, source) VALUES"
                     " ('2026-07-01', 'pre-migration')")
        conn.execute("INSERT INTO audit (ts, event) VALUES"
                     " ('2026-07-01', 'pre-migration-event')")
        conn.execute("INSERT INTO links (session_id, incident_key) VALUES"
                     " ('s0', 'INC-0')")

    reports = {r.name: r for r in migrate_legacy.migrate(home)}
    assert reports["flaky_detective"].tables["scan_runs"] == 1
    assert reports["flaky_healer"].tables == {"healer_runs": 1, "recipes": 1, "audit": 1}
    assert reports["jira_incidents"].tables["links"] == 1

    counts = _state_counts()
    # 1 pre-existing unified row + 1 legacy row each: nothing vanished.
    assert counts["scan_runs"] == 2
    assert counts["healer_runs"] == 2
    assert counts["audit"] == 2
    assert counts["links"] == 2
    with closing(state.connect()) as conn:
        events = {r["event"] for r in conn.execute("SELECT event FROM audit")}
    assert events == {"pre-migration-event", "pre_approval_request"}


def test_attach_ro_write_protects_and_aborts_rather_than_degrade(legacy_home):
    """The read-only promise (D4): attached legacy sources reject writes on
    this connection, and a build that refuses the mode=ro URI attach makes
    migration ABORT instead of falling back to a writable attach (which could
    checkpoint a WAL-mode source on detach)."""
    home, _counts, _parts = legacy_home
    legacy_db = home / "hermes-jira-incidents.db"

    # (a) writes through the attach are refused
    with closing(state.connect(uri=True)) as conn:
        migrate_legacy._attach_ro(conn, legacy_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO legacy.meta VALUES ('x', 'y')")
        finally:
            migrate_legacy._detach(conn)

    # (b) a rejected read-only ATTACH aborts loudly — never a writable fallback
    class NoUriAttachConn:
        """Wraps a real connection; refuses URI-filename ATTACHes the way an
        SQLite build without URI support would."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *params):
            if sql.startswith("ATTACH") and params and \
                    str(params[0][0]).startswith("file:"):
                raise sqlite3.OperationalError("unable to open database file")
            return self._real.execute(sql, *params)

    with closing(state.connect(uri=True)) as conn:
        with pytest.raises(migrate_legacy.MigrationError, match="read-only"):
            migrate_legacy._attach_ro(NoUriAttachConn(conn), legacy_db)

    # (c) an attach that silently comes up WRITABLE (mode=ro ignored) is
    # detected by the write probe and aborted too — never used for copying
    class SilentlyWritableAttachConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *params):
            if sql.startswith("ATTACH"):
                return self._real.execute(
                    "ATTACH DATABASE ? AS legacy", (str(legacy_db),))
            return self._real.execute(sql, *params)

    with closing(state.connect(uri=True)) as conn:
        with pytest.raises(migrate_legacy.MigrationError, match="writable"):
            migrate_legacy._attach_ro(SilentlyWritableAttachConn(conn), legacy_db)


def test_cli_migrate_reports_migration_error_as_failure(profile_env, monkeypatch,
                                                        capsys):
    import argparse

    from hermes_flaky_stabilization import cli
    from hermes_flaky_stabilization.storage import migrate_legacy as ml

    def refuse(**kwargs):
        raise ml.MigrationError("cannot attach legacy DB read-only (probe)")

    monkeypatch.setattr(ml, "migrate", refuse)
    parser = argparse.ArgumentParser(prog="flaky-stab")
    cli.setup_cli(parser)
    assert cli.run_cli(parser.parse_args(["migrate"])) == 1
    # Errors go to stderr: a no-agent cron job delivers stdout verbatim as its
    # report, so an abort on stdout would be delivered as though it were one.
    assert "migration aborted" in capsys.readouterr().err


def test_cli_migrate_and_status(legacy_home, capsys):
    import argparse

    from hermes_flaky_stabilization import cli

    parser = argparse.ArgumentParser(prog="flaky-stab")
    cli.setup_cli(parser)

    assert cli.run_cli(parser.parse_args(["migrate", "--dry-run"])) == 0
    out = capsys.readouterr().out
    assert "dry-run: nothing was written" in out

    assert cli.run_cli(parser.parse_args(["migrate"])) == 0
    out = capsys.readouterr().out
    assert "migration complete" in out
    assert "flaky_verdicts=2" in out

    assert cli.run_cli(parser.parse_args(["status"])) == 0
    status = capsys.readouterr().out
    assert "state.db" in status and "history.db" in status
    assert "incidents=2" in status
    assert "GITHUB_TOKEN set:   False" in status
    assert "JIRA_API_TOKEN set: False" in status


def test_install_cron_with_jira_sync(profile_env, monkeypatch, capsys):
    import argparse
    from types import SimpleNamespace

    from hermes_flaky_stabilization import cli

    created = []

    def fake_run(cmd, capture_output=False, text=False):
        created.append(cmd)
        return SimpleNamespace(returncode=0, stdout="created", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    parser = argparse.ArgumentParser(prog="flaky-stab")
    cli.setup_cli(parser)
    rc = cli.run_cli(parser.parse_args(
        ["install-cron", "--deliver", "local", "--with-jira-sync"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "flaky-stab-scan.sh" in out and "flaky-stab-jira-sync.sh" in out
    names = [cmd[cmd.index("--name") + 1] for cmd in created]
    assert names == ["flaky-stabilization", "flaky-stabilization-jira-sync"]

    # The scripts dir holds executables the gateway runs on a schedule: owner-only.
    # (In this flow the detection job's installer runs first and already sets 0700,
    # so this asserts the end state, not the jira installer's own chmod — that is
    # pinned directly on the shared helper in tests/test_cronjobs.py.)
    import os as _os
    import stat as _stat
    scripts_dir = profile_env / "scripts"
    assert _stat.S_IMODE(_os.stat(scripts_dir).st_mode) == 0o700

    shim = profile_env / "scripts" / "flaky-stab-jira-sync.sh"
    assert shim.exists()
    content = shim.read_text(encoding="utf-8")
    # D10: --quiet keeps successful ticks silent (no nightly delivery noise);
    # exec propagates the sync's exit code so failures still alert.
    assert "exec hermes flaky-stab jira sync --quiet" in content
    assert "set -euo pipefail" in content


def test_install_cron_with_jira_sync_missing_shim_is_a_clean_error(
    profile_env, monkeypatch, capsys
):
    """A site-packages install has no checkout scripts/ dir.

    The jira-sync installer used to `shutil.copyfile` with neither an existence
    check nor an OSError guard, so it raised an uncaught FileNotFoundError out of
    run_cli. It must now report a one-line error and exit non-zero, exactly like
    the detection job's installer already did.
    """
    import argparse
    from types import SimpleNamespace

    from hermes_flaky_stabilization import cli

    def fake_run(cmd, capture_output=False, text=False):
        return SimpleNamespace(returncode=0, stdout="created", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(cli, "JIRA_SYNC_SHIM", "no-such-jira-shim.sh")

    parser = argparse.ArgumentParser(prog="flaky-stab")
    cli.setup_cli(parser)
    rc = cli.run_cli(parser.parse_args(
        ["install-cron", "--deliver", "local", "--with-jira-sync"]))

    assert rc == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err and "no-such-jira-shim.sh" in captured.err
    assert "Traceback" not in captured.err
