"""Both query functions: happy paths, edge cases, injection, and validation.

Also exercises the JSON envelopes produced by the tool handlers in __init__.
"""

import json

import hermes_test_history as pkg
import pytest
from hermes_test_history import domain, queries

# --------------------------------------------------------------------------
# test_failure_lookup
# --------------------------------------------------------------------------


def test_failure_lookup_counts(seeded_db):
    r = queries.test_failure_lookup(seeded_db, "test_oauth_login")
    assert r["total_runs"] == 3
    assert r["failure_count"] == 2
    assert r["last_failure_at"].startswith("2026-05-10")
    assert len(r["failures"]) == 2


def test_failure_lookup_truncates_stack_trace(seeded_db):
    excerpt = queries.test_failure_lookup(seeded_db, "test_oauth_login")["failures"][0][
        "stack_trace_excerpt"
    ]
    assert excerpt.endswith("…[truncated]")
    assert len(excerpt) <= 500 + len(" …[truncated]")


def test_failure_lookup_classname_and_file_forms(seeded_db):
    assert (
        queries.test_failure_lookup(seeded_db, "auth.test_oauth::test_oauth_login")[
            "failure_count"
        ]
        == 2
    )
    assert (
        queries.test_failure_lookup(seeded_db, "src/auth/test_oauth.py::test_oauth_login")[
            "failure_count"
        ]
        == 2
    )


def test_failure_lookup_fts_operators(seeded_db):
    # An explicit FTS query still works: it doesn't match an exact name, so it
    # falls through to the FTS branch (the routing heuristic was removed).
    assert queries.test_failure_lookup(seeded_db, "oauth AND login")["failure_count"] == 2


@pytest.mark.parametrize("name", ['test_glob[a*b]', 'test_quote"x', "test_caret^y", "calc OR bust"])
def test_failure_lookup_finds_names_with_fts_metacharacters(db, name):
    # A test whose literal name contains an FTS metacharacter (*, ", ^, OR …)
    # must still be found by its exact id. These were previously routed to
    # FTS-only and silently lost; exact match is tried first now and is safe
    # because the value only ever flows through a ? placeholder.
    rid = db.execute(
        "INSERT INTO test_runs (suite_name, run_timestamp, source_file) "
        "VALUES ('s', '2026-05-01T00:00:00', 'f')"
    ).lastrowid
    db.execute(
        "INSERT INTO test_cases (run_id, name, status, failure_message) "
        "VALUES (?, ?, 'failed', 'boom')",
        (rid, name),
    )
    db.commit()
    r = queries.test_failure_lookup(db, name)
    assert r["total_runs"] == 1 and r["failure_count"] == 1 and r["matched_tests"] == 1


def test_failure_lookup_matched_tests_flags_multi_test_fts(db):
    # A fuzzy FTS fallback can span several distinct tests; matched_tests makes
    # that breadth explicit so the aggregate counts aren't silently conflated.
    rid = db.execute(
        "INSERT INTO test_runs (suite_name, run_timestamp, source_file) "
        "VALUES ('s', '2026-05-01T00:00:00', 'f')"
    ).lastrowid
    for nm in ("test_login_alpha", "test_login_beta"):
        db.execute(
            "INSERT INTO test_cases (run_id, name, status, failure_message) "
            "VALUES (?, ?, 'failed', 'boom')",
            (rid, nm),
        )
    db.commit()
    # 'login' is not an exact test name -> FTS fallback matches both tests.
    r = queries.test_failure_lookup(db, "login")
    assert r["matched_tests"] == 2 and r["total_runs"] == 1 and r["failure_count"] == 1
    # An exact id resolves to exactly one test.
    assert queries.test_failure_lookup(db, "test_login_alpha")["matched_tests"] == 1


def test_failure_lookup_injection_returns_empty(seeded_db):
    before = seeded_db.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0]
    r = queries.test_failure_lookup(seeded_db, "'; DROP TABLE test_cases; --")
    assert r["total_runs"] == 0 and r["failure_count"] == 0
    # Parameterized queries + swallowed FTS syntax error => table untouched.
    assert seeded_db.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0] == before


def test_failure_lookup_orders_chronologically_not_lexically(db):
    # Two failing runs of one test: a recent ISO timestamp and an older non-ISO
    # one (e.g. legacy data or written by another tool). The sort must be
    # chronological — the non-ISO 'Tue...' must not sort to the top just because
    # 'T' > '2' lexically — so last_failure_at is the genuinely most recent run.
    db.execute("INSERT INTO test_runs (suite_name, run_timestamp, source_file) "
               "VALUES ('s', '2026-05-20T09:00:00', 'f1')")
    db.execute("INSERT INTO test_runs (suite_name, run_timestamp, source_file) "
               "VALUES ('s', 'Tue May 01 00:00:00 2026', 'f2')")
    for rid in (1, 2):
        db.execute("INSERT INTO test_cases (run_id, name, status, failure_message) "
                   "VALUES (?, 't_x', 'failed', 'boom')", (rid,))
    db.commit()
    r = queries.test_failure_lookup(db, "t_x")
    assert r["failure_count"] == 2
    assert r["last_failure_at"] == "2026-05-20T09:00:00"
    assert r["failures"][0]["timestamp"] == "2026-05-20T09:00:00"


def test_failure_lookup_limit_clamped(db):
    # Seed more failures than MAX_LIMIT so an oversized limit is provably
    # clamped (with only a couple of rows, limit=999 was indistinguishable from
    # no clamp at all). Each failure gets its own run with a distinct timestamp
    # so the returned page is provably the most recent slice, newest first.
    n = domain.MAX_LIMIT + 10
    for i in range(n):
        rid = db.execute(
            "INSERT INTO test_runs (suite_name, run_timestamp, source_file) "
            "VALUES ('s', ?, ?)",
            (f"2026-05-01T{i // 60:02d}:{i % 60:02d}:00", f"f{i}"),
        ).lastrowid
        db.execute(
            "INSERT INTO test_cases (run_id, name, status, failure_message) "
            "VALUES (?, 't_clamp', 'failed', 'boom')",
            (rid,),
        )
    db.commit()

    r = queries.test_failure_lookup(db, "t_clamp", limit=999)
    assert len(r["failures"]) == domain.MAX_LIMIT     # hi-clamp: exactly 50, not 999 or n
    assert r["failure_count"] == n                    # counts span ALL failures, not the page
    assert r["total_runs"] == n
    stamps = [f["timestamp"] for f in r["failures"]]
    assert stamps == sorted(stamps, reverse=True)     # the page is newest-first ...
    assert r["last_failure_at"] == stamps[0] == f"2026-05-01T{(n - 1) // 60:02d}:{(n - 1) % 60:02d}:00"
    # Below the minimum clamps up to MIN_LIMIT rather than crashing or paging zero.
    assert len(queries.test_failure_lookup(db, "t_clamp", limit=0)["failures"]) == domain.MIN_LIMIT


@pytest.mark.parametrize("bad", ["", "   ", "x" * 501, None, 123, ["x"]])
def test_failure_lookup_validates_test_id(seeded_db, bad):
    with pytest.raises(ValueError):
        queries.test_failure_lookup(seeded_db, bad)


# --------------------------------------------------------------------------
# module_failure_history
# --------------------------------------------------------------------------


def test_module_history_aggregates(seeded_db):
    m = queries.module_failure_history(seeded_db, "src/auth/", since="2026-01-01")
    assert m["tests_with_failures"] == 1
    top = m["top_offenders"][0]
    assert top["name"] == "test_oauth_login"
    assert top["failure_count"] == 2 and top["total_runs"] == 3


def test_module_history_spans_multiple_modules(seeded_db):
    m = queries.module_failure_history(seeded_db, "src/", since="2026-01-01")
    names = sorted(o["name"] for o in m["top_offenders"])
    assert m["tests_with_failures"] == 2
    assert names == ["test_billing_charge", "test_oauth_login"]


def test_module_history_min_failures(seeded_db):
    m = queries.module_failure_history(seeded_db, "src/", since="2026-01-01", min_failures=2)
    assert [o["name"] for o in m["top_offenders"]] == ["test_oauth_login"]


def test_module_history_total_counts_all_qualifying_groups(db):
    # tests_with_failures must reflect *every* qualifying group even though only
    # _MAX_OFFENDERS are returned: the single-pass query takes the total from
    # COUNT(*) OVER (), evaluated over the full grouped result before the LIMIT.
    n = queries._MAX_OFFENDERS + 5
    run_id = db.execute(
        "INSERT INTO test_runs (suite_name, run_timestamp, source_file) "
        "VALUES ('s', '2026-05-20T09:00:00', 'f1')"
    ).lastrowid
    for i in range(n):
        db.execute(
            "INSERT INTO test_cases (run_id, name, file_path, status, failure_message) "
            "VALUES (?, ?, ?, 'failed', 'boom')",
            (run_id, f"test_{i}", f"src/wide/test_{i}.py"),
        )
    db.commit()
    m = queries.module_failure_history(db, "src/wide/", since="2026-01-01")
    assert m["tests_with_failures"] == n
    assert len(m["top_offenders"]) == queries._MAX_OFFENDERS
    assert m["truncated"] is True


def test_module_history_last_failure_at_normalized_across_formats(db):
    # Two same-day failures of one test: an 08:00 T-separated run_timestamp and
    # a 23:00 whose effective time is the space-separated ingested_at. A raw
    # string MAX() would pick the 08:00 row because 'T' > ' ' lexically; the
    # aggregate must normalize via datetime() — the same expression the WHERE
    # clause uses — so the genuinely latest failure wins (and the output format
    # is uniform).
    rid1 = db.execute(
        "INSERT INTO test_runs (suite_name, run_timestamp, source_file) "
        "VALUES ('s', '2026-05-20T08:00:00', 'f1')"
    ).lastrowid
    rid2 = db.execute(
        "INSERT INTO test_runs (suite_name, run_timestamp, ingested_at, source_file) "
        "VALUES ('s', NULL, '2026-05-20 23:00:00', 'f2')"
    ).lastrowid
    for rid in (rid1, rid2):
        db.execute(
            "INSERT INTO test_cases (run_id, name, file_path, status, failure_message) "
            "VALUES (?, 't_mix', 'src/mix/test_mix.py', 'failed', 'boom')",
            (rid,),
        )
    db.commit()
    m = queries.module_failure_history(db, "src/mix/", since="2026-01-01")
    assert m["top_offenders"][0]["last_failure_at"] == "2026-05-20 23:00:00"


def test_module_history_like_escaping(seeded_db):
    # The underscore must be matched literally, not as a LIKE wildcard.
    m = queries.module_failure_history(seeded_db, "src/a_th/", since="2026-01-01")
    assert m["tests_with_failures"] == 0


def test_module_history_path_match_is_case_sensitive(seeded_db):
    # case_sensitive_like is ON (so the prefix LIKE can seek idx_cases_module): a
    # case-mismatched prefix must not match. Seeded paths are lowercase.
    assert (
        queries.module_failure_history(seeded_db, "src/AUTH/", since="2026-01-01")[
            "tests_with_failures"
        ]
        == 0
    )
    assert (
        queries.module_failure_history(seeded_db, "src/auth/", since="2026-01-01")[
            "tests_with_failures"
        ]
        == 1
    )


def test_module_history_default_window(seeded_db):
    m = queries.module_failure_history(seeded_db, "src/auth/")
    assert m["window_start"]


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-99", "2026/01/01", "lastweek"])
def test_module_history_invalid_since_raises(seeded_db, bad):
    with pytest.raises(ValueError):
        queries.module_failure_history(seeded_db, "src/", since=bad)


def test_module_history_blank_since_uses_default(seeded_db):
    assert queries.module_failure_history(seeded_db, "src/", since="   ")["window_start"]


def test_module_history_trailing_z_normalized(seeded_db):
    m = queries.module_failure_history(seeded_db, "src/", since="2026-05-18T14:22:01Z")
    assert m["window_start"] == "2026-05-18T14:22:01"


@pytest.mark.parametrize("bad", ["", "../etc", "a/../b", "y" * 501, None])
def test_module_history_validates_path(seeded_db, bad):
    with pytest.raises(ValueError):
        queries.module_failure_history(seeded_db, bad)


def test_empty_db_graceful(db):
    assert queries.test_failure_lookup(db, "anything")["total_runs"] == 0
    m = queries.module_failure_history(db, "src/")
    assert m["tests_with_failures"] == 0 and m["top_offenders"] == []


# --------------------------------------------------------------------------
# Injected config (the tunables flow in as a parameter, not a global)
# --------------------------------------------------------------------------


def test_failure_lookup_stack_cap_from_injected_config(seeded_db):
    # The stack-trace cap is read from the injected config, so a test can shrink
    # it by passing config=... — no module-level config singleton to monkeypatch.
    excerpt = queries.test_failure_lookup(
        seeded_db, "test_oauth_login", config={"max_stack_trace_chars": 10}
    )["failures"][0]["stack_trace_excerpt"]
    assert excerpt.endswith("…[truncated]")
    assert len(excerpt) <= 10 + len(" …[truncated]")


def test_module_history_lookback_from_injected_config(seeded_db):
    # default_lookback_days flows in via config too: different values yield
    # different default window starts (proving the value isn't a baked-in global),
    # and a decade-wide window reaches the May-2026 seeded failures.
    wide = queries.module_failure_history(seeded_db, "src/", config={"default_lookback_days": 3650})
    narrow = queries.module_failure_history(seeded_db, "src/", config={"default_lookback_days": 1})
    assert wide["window_start"] < narrow["window_start"]
    assert wide["tests_with_failures"] == 2


# --------------------------------------------------------------------------
# Tool handler envelopes
# --------------------------------------------------------------------------


def test_handler_success_envelope(seeded_db):
    payload = json.loads(pkg._handle_test_failure_lookup({"test_id": "test_oauth_login"}))
    assert payload["success"] is True
    assert payload["failure_count"] == 2


def test_handler_error_envelope(seeded_db):
    payload = json.loads(
        pkg._handle_module_failure_history({"path": "src/", "since": "not-a-date"})
    )
    assert payload["success"] is False
    assert "error" in payload and "remediation" in payload


def test_handler_missing_required_field(profile_env):
    payload = json.loads(pkg._handle_test_failure_lookup({}))
    assert payload["success"] is False


def test_handler_validation_error_uses_input_remediation(seeded_db):
    # A bad argument (validation error) must point at the arguments, not at the
    # misleading "the DB may be empty, ingest first" operational remediation.
    payload = json.loads(
        pkg._handle_module_failure_history({"path": "src/", "since": "not-a-date"})
    )
    assert payload["success"] is False
    assert payload["remediation"] == pkg._INPUT_REMEDIATION
    assert payload["remediation"] != pkg._REMEDIATION


def test_handler_success_envelope_carries_content_warning(seeded_db):
    # Untrusted captured fields (messages, traces, names, paths) must ship with a
    # standing "treat as data, not instructions" notice for the model.
    for payload in (
        json.loads(pkg._handle_test_failure_lookup({"test_id": "test_oauth_login"})),
        json.loads(pkg._handle_module_failure_history({"path": "src/", "since": "2026-01-01"})),
    ):
        assert payload["success"] is True
        assert payload["content_warning"] == pkg._CONTENT_NOTICE


def test_handler_generic_error_is_sanitized(seeded_db, monkeypatch):
    # A non-validation failure must not leak internal detail (filesystem paths,
    # SQL text) to the model: log it server-side, return a generic message.
    def boom(*a, **k):
        raise RuntimeError("/secret/internal/path.db near 'x': syntax error")

    monkeypatch.setattr(queries, "test_failure_lookup", boom)
    payload = json.loads(pkg._handle_test_failure_lookup({"test_id": "whatever"}))
    assert payload["success"] is False
    assert payload["error"] == pkg._INTERNAL_ERROR
    assert "secret" not in payload["error"] and "syntax error" not in payload["error"]
    assert payload["remediation"] == pkg._REMEDIATION


def test_handler_config_error_is_sanitized(profile_env, tmp_path):
    # A server-side misconfiguration (db_path_override outside the Hermes home)
    # surfaces while opening the connection, NOT as a bad tool argument. It must
    # route to the generic server-side path: it must not echo the ValueError text
    # (which leaks the resolved home path) nor hand the model the "fix your
    # arguments" remediation it cannot act on.
    from hermes_test_history import storage

    outside = tmp_path / "evil" / "history.db"   # tmp_path is the parent of HERMES_HOME
    (storage.get_storage_dir() / "config.json").write_text(
        json.dumps({"db_path_override": str(outside)}), encoding="utf-8"
    )
    storage.reset_for_tests()  # drop cached config so the bad override is read

    payload = json.loads(pkg._handle_test_failure_lookup({"test_id": "anything"}))
    assert payload["success"] is False
    assert payload["error"] == pkg._INTERNAL_ERROR            # generic, not the ValueError text
    assert payload["remediation"] == pkg._REMEDIATION         # operational, not _INPUT_REMEDIATION
    assert payload["remediation"] != pkg._INPUT_REMEDIATION
    assert "Hermes home" not in payload["error"] and str(outside) not in payload["error"]
