"""Unit tests for the pure detection core (``detect.compute_verdicts``).

These are the most important tests in the suite: they pin the flaky/consistently-
failing/stable classification, the ``include_errors`` toggle, ``skipped``
handling, the exact-threshold boundary, and the first/last/last-failure tracking
— all with plain tuples and no database.
"""

from datetime import UTC, datetime

from hermes_flaky_detective import detect, domain

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _verdicts(rows, *, window_days=14, min_fails=3, include_errors=True):
    return {v.test_key: v for v in
            detect.compute_verdicts(rows, NOW, window_days, min_fails, include_errors)}


def _row(status, ts, classname="pkg.mod", name="test_a", file_path="src/mod.py"):
    return (classname, name, file_path, status, ts)


# ---------------------------------------------------------------------------
# Core classification
# ---------------------------------------------------------------------------


def test_clean_flaky_case():
    rows = [
        _row("failed", "2026-05-20T09:00:00"),
        _row("failed", "2026-05-21T09:00:00"),
        _row("failed", "2026-05-22T09:00:00"),
        _row("passed", "2026-05-23T09:00:00"),
        _row("passed", "2026-05-24T09:00:00"),
    ]
    v = _verdicts(rows)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_FLAKY
    assert (v.passes, v.fails, v.runs) == (2, 3, 5)
    assert v.window_days == 14


def test_consistently_failing_case():
    rows = [_row("failed", f"2026-05-2{i}T09:00:00") for i in range(1, 5)]  # 4 fails, 0 pass
    v = _verdicts(rows)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_CONSISTENTLY_FAILING
    assert (v.passes, v.fails) == (0, 4)


def test_stable_below_threshold():
    rows = [
        _row("failed", "2026-05-20T09:00:00"),
        _row("failed", "2026-05-21T09:00:00"),  # only 2 fails < min_fails(3)
        _row("passed", "2026-05-22T09:00:00"),
    ]
    v = _verdicts(rows)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_STABLE
    assert (v.passes, v.fails, v.runs) == (1, 2, 3)


# ---------------------------------------------------------------------------
# error-status handling (Phase 0 Q2)
# ---------------------------------------------------------------------------


def test_error_counts_as_failure_when_enabled():
    rows = [
        _row("failed", "2026-05-20T09:00:00"),
        _row("failed", "2026-05-21T09:00:00"),
        _row("error", "2026-05-22T09:00:00"),   # 3rd failure only if errors count
        _row("passed", "2026-05-23T09:00:00"),
    ]
    v = _verdicts(rows, include_errors=True)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_FLAKY
    assert (v.passes, v.fails, v.runs) == (1, 3, 4)


def test_error_excluded_when_disabled():
    rows = [
        _row("failed", "2026-05-20T09:00:00"),
        _row("failed", "2026-05-21T09:00:00"),
        _row("error", "2026-05-22T09:00:00"),   # ignored entirely
        _row("passed", "2026-05-23T09:00:00"),
    ]
    v = _verdicts(rows, include_errors=False)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_STABLE
    # the error row is neither a pass nor a fail, so runs excludes it
    assert (v.passes, v.fails, v.runs) == (1, 2, 3)


def test_only_errors_consistently_failing_when_enabled():
    rows = [_row("error", f"2026-05-2{i}T09:00:00") for i in range(1, 5)]
    v_on = _verdicts(rows, include_errors=True)["pkg.mod::test_a"]
    assert v_on.status == domain.VERDICT_CONSISTENTLY_FAILING
    assert (v_on.passes, v_on.fails) == (0, 4)
    # With errors disabled the same rows are all ignored -> stable, zero runs.
    v_off = _verdicts(rows, include_errors=False)["pkg.mod::test_a"]
    assert v_off.status == domain.VERDICT_STABLE
    assert (v_off.passes, v_off.fails, v_off.runs) == (0, 0, 0)


# ---------------------------------------------------------------------------
# skipped handling
# ---------------------------------------------------------------------------


def test_skipped_is_ignored():
    rows = [
        _row("passed", "2026-05-20T09:00:00"),
        _row("skipped", "2026-05-21T09:00:00"),  # never counted (defensive)
        _row("skipped", "2026-05-22T09:00:00"),
    ]
    v = _verdicts(rows)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_STABLE
    assert (v.passes, v.fails, v.runs) == (1, 0, 1)
    # skipped timestamps must not move first/last_seen
    assert v.first_seen == "2026-05-20T09:00:00"
    assert v.last_seen == "2026-05-20T09:00:00"


# ---------------------------------------------------------------------------
# exact-threshold boundary
# ---------------------------------------------------------------------------


def test_boundary_exactly_min_fails_with_pass_is_flaky():
    rows = [_row("failed", f"2026-05-2{i}T09:00:00") for i in range(1, 4)]  # exactly 3
    rows.append(_row("passed", "2026-05-24T09:00:00"))
    v = _verdicts(rows, min_fails=3)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_FLAKY


def test_boundary_one_below_threshold_is_stable():
    rows = [_row("failed", "2026-05-21T09:00:00"), _row("failed", "2026-05-22T09:00:00")]
    rows.append(_row("passed", "2026-05-24T09:00:00"))
    v = _verdicts(rows, min_fails=3)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_STABLE


# ---------------------------------------------------------------------------
# The core trusts pre-filtered input (no windowing of its own)
# ---------------------------------------------------------------------------


def test_core_does_not_re_window_input():
    # Timestamps years before `now`: the core must still count them, proving the
    # window filter lives in the reader, not here.
    rows = [_row("failed", f"2019-01-0{i}T09:00:00") for i in range(1, 4)]
    rows.append(_row("passed", "2019-01-04T09:00:00"))
    v = _verdicts(rows)["pkg.mod::test_a"]
    assert v.status == domain.VERDICT_FLAKY
    assert v.runs == 4


# ---------------------------------------------------------------------------
# first/last/last-failure tracking
# ---------------------------------------------------------------------------


def test_tracks_first_last_and_last_failure():
    rows = [
        _row("passed", "2026-05-10T09:00:00"),   # earliest
        _row("failed", "2026-05-12T09:00:00"),
        _row("failed", "2026-05-15T09:00:00"),
        _row("failed", "2026-05-18T09:00:00"),   # latest failure
        _row("passed", "2026-05-20T09:00:00"),   # latest overall
    ]
    v = _verdicts(rows)["pkg.mod::test_a"]
    assert v.first_seen == "2026-05-10T09:00:00"
    assert v.last_seen == "2026-05-20T09:00:00"
    assert v.last_failure == "2026-05-18T09:00:00"


def test_stable_test_has_no_last_failure():
    rows = [_row("passed", "2026-05-10T09:00:00"), _row("passed", "2026-05-11T09:00:00")]
    v = _verdicts(rows)["pkg.mod::test_a"]
    assert v.last_failure is None


# ---------------------------------------------------------------------------
# test identity / multi-test
# ---------------------------------------------------------------------------


def test_empty_classname_yields_colon_colon_key():
    # jest-style: classname omitted (None) -> key is "::name", file_path NULL.
    rows = [
        ("", "renders header", None, "failed", "2026-05-20T09:00:00"),
        (None, "renders header", None, "failed", "2026-05-21T09:00:00"),
        (None, "renders header", None, "failed", "2026-05-22T09:00:00"),
        (None, "renders header", None, "passed", "2026-05-23T09:00:00"),
    ]
    verdicts = _verdicts(rows)
    assert "::renders header" in verdicts
    v = verdicts["::renders header"]
    assert v.status == domain.VERDICT_FLAKY
    assert v.classname in ("", None)
    assert v.name == "renders header"


def test_file_path_backfilled_from_later_row():
    rows = [
        ("pkg.mod", "test_a", None, "failed", "2026-05-20T09:00:00"),
        ("pkg.mod", "test_a", "src/mod.py", "failed", "2026-05-21T09:00:00"),
        ("pkg.mod", "test_a", None, "failed", "2026-05-22T09:00:00"),
        ("pkg.mod", "test_a", None, "passed", "2026-05-23T09:00:00"),
    ]
    v = _verdicts(rows)["pkg.mod::test_a"]
    assert v.file_path == "src/mod.py"


def test_multiple_tests_distinct_verdicts_and_order():
    rows = [
        ("a.T", "test_flaky", "a.py", "failed", "2026-05-20T09:00:00"),
        ("a.T", "test_flaky", "a.py", "failed", "2026-05-21T09:00:00"),
        ("a.T", "test_flaky", "a.py", "failed", "2026-05-22T09:00:00"),
        ("a.T", "test_flaky", "a.py", "passed", "2026-05-23T09:00:00"),
        ("b.T", "test_stable", "b.py", "passed", "2026-05-20T09:00:00"),
    ]
    out = detect.compute_verdicts(rows, NOW, 14, 3, True)
    assert [v.test_key for v in out] == ["a.T::test_flaky", "b.T::test_stable"]
    by_key = {v.test_key: v for v in out}
    assert by_key["a.T::test_flaky"].status == domain.VERDICT_FLAKY
    assert by_key["b.T::test_stable"].status == domain.VERDICT_STABLE


def test_empty_input_yields_no_verdicts():
    assert detect.compute_verdicts([], NOW, 14, 3, True) == []


def test_classname_and_name_are_stripped_to_match_test_key():
    # A name/classname with surrounding whitespace must be stored stripped, so the
    # stored columns agree with test_key (built from stripped values). Otherwise the
    # is_flaky bare-name / file_path::name lookups (which match the name column) miss.
    rows = [
        ("  pkg.Mod  ", "  test_a  ", "src/mod.py", "failed", "2026-05-20T09:00:00"),
        ("  pkg.Mod  ", "  test_a  ", "src/mod.py", "failed", "2026-05-21T09:00:00"),
        ("  pkg.Mod  ", "  test_a  ", "src/mod.py", "failed", "2026-05-22T09:00:00"),
        ("  pkg.Mod  ", "  test_a  ", "src/mod.py", "passed", "2026-05-23T09:00:00"),
    ]
    out = detect.compute_verdicts(rows, NOW, 14, 3, True)
    assert len(out) == 1
    v = out[0]
    assert v.test_key == "pkg.Mod::test_a"
    assert v.classname == "pkg.Mod"      # stored value matches the key, not " pkg.Mod "
    assert v.name == "test_a"
