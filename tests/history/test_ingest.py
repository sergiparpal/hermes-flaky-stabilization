"""Parser correctness across the three JUnit dialects, plus ingestion behavior.

Parsing lives in ``parser`` and persistence in ``ingest``; the tests address each
through its own module accordingly.
"""

from pathlib import Path

import pytest
from hermes_test_history import ingest, parser

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_parse_pytest_basic():
    run = parser.parse_junit_xml(FIXTURES / "pytest_junit_basic.xml")
    assert (run.total, run.failures, run.errors, run.skipped) == (3, 0, 0, 0)
    assert run.run_timestamp == "2026-05-01T10:00:00"
    assert {c.status for c in run.cases} == {"passed"}
    assert run.cases[0].file_path == "tests/test_math.py"
    assert run.suite_name == "pytest"


def test_parse_pytest_failures_counts_and_statuses():
    run = parser.parse_junit_xml(FIXTURES / "pytest_junit_failures.xml")
    assert (run.total, run.failures, run.errors, run.skipped) == (5, 2, 1, 1)
    by = {c.name: c for c in run.cases}

    login = by["test_oauth_login"]
    assert login.status == "failed"
    assert login.failure_type == "AssertionError"
    assert "token expired" in login.failure_message
    assert login.stack_trace and "Traceback" in login.stack_trace
    assert login.file_path == "src/auth/test_oauth.py"
    assert login.line_number == 10
    assert abs(login.duration_ms - 500.0) < 1e-6

    assert by["test_oauth_logout"].status == "error"
    assert by["test_oauth_skipme"].status == "skipped"
    assert by["test_oauth_ok"].status == "passed"


def test_parse_jest_classname_based_no_file():
    run = parser.parse_junit_xml(FIXTURES / "jest_junit.xml")
    assert (run.total, run.failures, run.errors, run.skipped) == (3, 1, 0, 0)
    assert all(c.file_path is None for c in run.cases)
    by = {c.name: c for c in run.cases}
    assert by["rejects an expired token"].status == "failed"
    assert by["rejects an expired token"].classname == "OAuth flow"


def test_empty_string_attrs_become_none(db, tmp_path):
    p = tmp_path / "blanks.xml"
    p.write_text(
        '<testsuite name="x" tests="1" failures="0" errors="0" skipped="0" '
        'timestamp="2026-05-01T00:00:00">'
        '<testcase classname="" name="t" file="" line="" time=""/>'
        "</testsuite>"
    )
    run = parser.parse_junit_xml(p)
    c = run.cases[0]
    assert c.classname is None and c.file_path is None
    assert c.line_number is None and c.duration_ms is None


def test_ingest_file_persists_counts(db):
    run_id = ingest.ingest_file(db, FIXTURES / "pytest_junit_failures.xml")
    row = db.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
    assert (row["total"], row["failures"], row["errors"], row["skipped"]) == (5, 2, 1, 1)
    assert Path(row["source_file"]).is_absolute()
    assert row["source_file"].endswith("pytest_junit_failures.xml")
    n = db.execute("SELECT COUNT(*) FROM test_cases WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert n == 5


def test_reingest_creates_second_run_no_dedup(db):
    first = ingest.ingest_file(db, FIXTURES / "pytest_junit_basic.xml")
    second = ingest.ingest_file(db, FIXTURES / "pytest_junit_basic.xml")
    assert first != second
    assert db.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0] == 2


def test_ingest_directory_recursive_skips_malformed(db, tmp_path):
    (tmp_path / "good.xml").write_text((FIXTURES / "pytest_junit_basic.xml").read_text())
    (tmp_path / "bad.xml").write_text("<<< not xml at all >>>")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "jest.xml").write_text((FIXTURES / "jest_junit.xml").read_text())

    ids, skipped = ingest.ingest_directory(db, tmp_path)
    assert len(ids) == 2  # good.xml + nested/jest.xml; bad.xml skipped, never raises
    assert skipped == 1   # bad.xml is reported as skipped, not silently dropped


def test_timestamp_falls_back_to_mtime(db, tmp_path):
    p = tmp_path / "no_ts.xml"
    p.write_text(
        '<testsuite name="x" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="c" name="t"/></testsuite>'
    )
    run = parser.parse_junit_xml(p)
    assert run.run_timestamp is not None
    assert "T" in run.run_timestamp  # ISO form from mtime fallback


def test_non_iso_timestamp_falls_back_to_mtime(db, tmp_path):
    # A non-ISO timestamp must NOT be stored verbatim: SQLite's datetime() can't
    # parse it, which would make the run invisible to every time-windowed query.
    # It must fall back to a parseable mtime ISO string instead.
    p = tmp_path / "weird_ts.xml"
    p.write_text(
        '<testsuite name="x" tests="1" failures="0" '
        'timestamp="Tue May 10 14:22:01 2026">'
        '<testcase classname="c" name="t"/></testsuite>'
    )
    run = parser.parse_junit_xml(p)
    assert run.run_timestamp != "Tue May 10 14:22:01 2026"
    from datetime import datetime
    datetime.fromisoformat(run.run_timestamp)  # parseable ISO, does not raise
    # And SQLite agrees it is a real datetime (not NULL).
    assert db.execute("SELECT datetime(?)", (run.run_timestamp,)).fetchone()[0] is not None


def test_nested_testsuite_counts_not_doubled(tmp_path):
    # <testsuite> nested inside <testsuite> must not double-count run totals.
    p = tmp_path / "nested.xml"
    p.write_text(
        '<testsuites tests="2" failures="0">'
        '  <testsuite name="outer" tests="2" failures="0">'
        '    <testsuite name="inner" tests="2" failures="0">'
        '      <testcase classname="c" name="a"/>'
        '      <testcase classname="c" name="b"/>'
        "    </testsuite>"
        "  </testsuite>"
        "</testsuites>"
    )
    run = parser.parse_junit_xml(p)
    assert len(run.cases) == 2          # cases collected from the nested suite
    assert run.total == 2               # not 4 — inner suite not summed twice


def test_non_junit_xml_is_rejected(db, tmp_path):
    # A well-formed but non-JUnit document must raise (and be skipped by a
    # directory ingest) rather than create an empty, useless run row.
    p = tmp_path / "not_junit.xml"
    p.write_text("<html><body><p>not a test report</p></body></html>")
    with pytest.raises(ValueError):
        parser.parse_junit_xml(p)

    ids, skipped = ingest.ingest_directory(db, tmp_path)
    assert ids == [] and skipped == 1
    assert db.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0] == 0


def test_billion_laughs_entity_expansion_is_blocked(db, tmp_path):
    # A nested-entity ("billion laughs") bomb must be refused at the DTD before a
    # single entity expands — otherwise ingesting one tiny crafted file is a
    # memory-exhaustion DoS. (Kept small here; the real bomb has more levels.)
    bomb = tmp_path / "bomb.xml"
    bomb.write_text(
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE testsuites [\n'
        '  <!ENTITY a "AAAAAAAAAA">\n'
        '  <!ENTITY b "&a;&a;&a;&a;&a;">\n'
        '  <!ENTITY c "&b;&b;&b;&b;&b;">\n'
        ']>\n'
        '<testsuites><testsuite name="s"><testcase name="&c;"/></testsuite></testsuites>'
    )
    with pytest.raises(ValueError):
        parser.parse_junit_xml(bomb)
    # A directory ingest skips it (never raises, never OOMs the process).
    ids, skipped = ingest.ingest_directory(db, tmp_path)
    assert ids == [] and skipped == 1


def test_external_entity_xxe_is_blocked(db, tmp_path):
    # An external-entity (XXE) document declares a DTD, which we forbid outright,
    # so it can never reach the filesystem/network.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENTS")
    evil = tmp_path / "xxe.xml"
    evil.write_text(
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE testsuites [ <!ENTITY xxe SYSTEM "file://{secret}"> ]>\n'
        '<testsuites><testsuite name="s">'
        '<testcase name="t">&xxe;</testcase></testsuite></testsuites>'
    )
    with pytest.raises(ValueError):
        parser.parse_junit_xml(evil)


def test_oversized_xml_is_rejected(db, tmp_path, monkeypatch):
    # Files larger than the cap are refused before a full-DOM parse loads the
    # whole tree into memory.
    monkeypatch.setattr(parser, "_MAX_XML_BYTES", 16)
    p = tmp_path / "big.xml"
    p.write_text('<testsuite name="x"><testcase name="t"/></testsuite>')
    with pytest.raises(ValueError):
        parser.parse_junit_xml(p)


def test_testsuite_root_collects_nested_testcases(tmp_path):
    # A <testsuite> ROOT with both a direct <testcase> and a nested <testsuite>
    # must collect cases from BOTH — the nested ones were previously dropped
    # (only the <testsuites> root walked nested suites).
    p = tmp_path / "nested_root.xml"
    p.write_text(
        '<testsuite name="outer">'
        '  <testcase classname="c" name="direct_case"/>'
        '  <testsuite name="inner">'
        '    <testcase classname="c" name="nested_case"/>'
        "  </testsuite>"
        "</testsuite>"
    )
    run = parser.parse_junit_xml(p)
    assert {c.name for c in run.cases} == {"direct_case", "nested_case"}


@pytest.mark.parametrize("bad", ["1e999", "inf", "nan", "1e308", "9" * 25])
def test_out_of_range_numeric_attrs_become_none(bad):
    # Absurd numeric attributes coerce to None (treated as absent) instead of
    # raising OverflowError — which would otherwise abort the parse or overflow
    # the SQLite bind. Real emitters write small integers; this hardens parsing.
    assert parser._to_int(bad) is None


def test_ingest_file_with_out_of_range_attrs_succeeds(db, tmp_path):
    # A file carrying absurd line/tests attributes must still ingest (the bad
    # values drop to None / fall back), not get discarded wholesale or crash.
    p = tmp_path / "huge.xml"
    p.write_text(
        '<testsuite name="s" tests="1e999" timestamp="2026-05-01T00:00:00">'
        '<testcase classname="c" name="t" line="1e308"/></testsuite>'
    )
    run_id = ingest.ingest_file(db, p)
    case = db.execute("SELECT * FROM test_cases WHERE run_id=?", (run_id,)).fetchone()
    assert case is not None and case["line_number"] is None   # 1e308 -> NULL, no crash
    assert db.execute("SELECT total FROM test_runs WHERE id=?", (run_id,)).fetchone()[0] == 1


def test_ingest_directory_insert_failure_leaves_no_orphan_run(db, tmp_path, monkeypatch):
    # If a case fails at INSERT time (not parse time), ingest_file must roll back
    # its own run row so a shared batch transaction is never left with an orphan
    # run that has zero cases — and the directory ingest must still continue.
    real = parser.parse_junit_xml

    def fake(path):
        if Path(path).name == "bad.xml":
            # line_number past SQLite's 64-bit INTEGER range fails only at bind,
            # i.e. AFTER the run row is staged (bypasses _to_int by construction).
            return parser.ParsedRun(
                suite_name="bad", source_file=str(path),
                run_timestamp="2026-05-01T00:00:00", total=1,
                cases=[parser.ParsedCase(name="t", status="failed", line_number=2 ** 63)],
            )
        return real(path)

    monkeypatch.setattr(parser, "parse_junit_xml", fake)
    (tmp_path / "good.xml").write_text((FIXTURES / "pytest_junit_basic.xml").read_text())
    (tmp_path / "bad.xml").write_text("<testsuite/>")

    ids, skipped = ingest.ingest_directory(db, tmp_path)
    assert len(ids) == 1 and skipped == 1               # bad file skipped, not fatal
    assert db.execute("SELECT COUNT(*) FROM test_runs").fetchone()[0] == 1   # no phantom run
    assert db.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0] == 3  # good file intact
    orphans = db.execute(
        "SELECT COUNT(*) FROM test_runs WHERE id NOT IN (SELECT DISTINCT run_id FROM test_cases)"
    ).fetchone()[0]
    assert orphans == 0
    # FTS index stays consistent with the base table after the rollback.
    assert (
        db.execute("SELECT COUNT(*) FROM test_cases_fts").fetchone()[0]
        == db.execute("SELECT COUNT(*) FROM test_cases").fetchone()[0]
    )
