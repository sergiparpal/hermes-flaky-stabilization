"""Trace parser tests over the real captured fixtures + the synthetic one
(plan Phase 2 acceptance: every fixture parses to action+selector+error)."""

import zipfile

import pytest
from conftest import FIXTURES
from flaky_healer.trace import parse_trace

TRACES = FIXTURES / "traces"


def test_selector_fixture_click_timeout():
    s = parse_trace(TRACES / "selector.trace.zip")
    assert s.failing_action == "click"
    assert s.selector == "#btn-1f9c"
    assert s.error_name == "TimeoutError"
    assert s.timeout_ms == 2000
    assert s.timed_out is True
    assert s.selector_resolved is False
    assert any("waiting for locator" in line for line in s.call_log)
    # the DOM snapshot proves a same-shaped element carries data-testid
    assert s.testid_on_target == "submit"
    assert "Timeout 2000ms exceeded" in s.test_error_message


def test_timeout_fixture_expect_resolved_no_network():
    s = parse_trace(TRACES / "timeout.trace.zip")
    assert s.failing_action == "expect.toHaveText"
    assert s.selector == "#delayed-banner"
    assert s.error_name == "TimeoutError"
    assert s.timeout_ms == 1500
    assert s.timed_out is True
    assert s.selector_resolved is True, "element exists; only its text lags"
    assert s.pending_requests == []


def test_race_fixture_expect_with_pending_request():
    s = parse_trace(TRACES / "race.trace.zip")
    assert s.failing_action == "expect.toHaveText"
    assert s.selector == "#item-count"
    assert s.timed_out is True
    assert s.selector_resolved is True
    assert [r.url for r in s.pending_requests] == ["http://127.0.0.1:4173"]
    assert s.pending_requests[0].pending is True


def test_dom_testids_unioned_across_snapshots():
    s = parse_trace(TRACES / "selector.trace.zip")
    assert {"submit", "banner", "load-items", "item-count"} <= set(s.dom_testids)
    # rotated-id button: id differs from #btn-1f9c but shape-matches btn-<h>
    submit_attrs = s.dom_testids["submit"]
    assert submit_attrs.get("id", "").startswith("btn-")


def test_ambiguous_synthetic_parses_without_timeout():
    s = parse_trace(TRACES / "ambiguous-synthetic.trace.zip")
    assert s.failing_action == "click"
    assert s.selector == "#widget"
    assert s.error_name == "Error"
    assert s.timed_out is False
    assert s.selector_resolved is True
    assert s.pending_requests == []
    assert s.testid_on_target is None


def test_trace_version_detected():
    s = parse_trace(TRACES / "race.trace.zip")
    assert s.trace_version == 8


def test_brief_is_json_safe():
    import json

    s = parse_trace(TRACES / "race.trace.zip")
    payload = json.dumps(s.brief())
    assert "expect.toHaveText" in payload


def test_corrupt_zip_raises_value_error(tmp_path):
    bad = tmp_path / "corrupt.trace.zip"
    bad.write_bytes(b"this is not a zip at all")
    with pytest.raises(ValueError, match="not a readable Playwright trace.zip"):
        parse_trace(bad)


def test_missing_file_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        parse_trace(tmp_path / "nope.trace.zip")


def test_zip_without_trace_entries_raises(tmp_path):
    empty = tmp_path / "empty.trace.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    with pytest.raises(ValueError, match="no .trace entries"):
        parse_trace(empty)
