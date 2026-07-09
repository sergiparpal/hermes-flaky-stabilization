"""Tests for the local CI-log pre-filter (plan Phase 1 acceptance)."""

import io
import zipfile

from conftest import FIXTURES
from flaky_healer.logfilter import filter_log


def _noise(n, prefix="npm http fetch GET 200 https://registry.npmjs.org/pkg"):
    return [f"{prefix}-{i} {30 + i % 40}ms" for i in range(n)]


def test_keeps_context_window_around_anchor():
    before = _noise(100, prefix="before-noise")
    after = _noise(100, prefix="after-noise")
    lines = before + ["Error: kaboom happened"] + after
    out = filter_log("\n".join(lines), context=40)
    assert "Error: kaboom happened" in out.text
    assert "before-noise-60 " in out.text  # within ±40 of the anchor
    assert "before-noise-1 " not in out.text  # far before the anchor window
    assert "after-noise-39 " in out.text
    assert "after-noise-80 " not in out.text
    assert out.anchor_count == 1
    assert out.bytes_filtered < out.bytes_raw


def test_merges_overlapping_windows_into_one_block():
    lines = _noise(60) + ["Error: first", "ok", "Error: second"] + _noise(60)
    out = filter_log("\n".join(lines), context=40)
    assert out.blocks_kept == 1
    assert "Error: first" in out.text and "Error: second" in out.text


def test_dedupes_repeated_failure_blocks():
    block = ["section start"] + _noise(5, prefix="setup line") + [
        "TimeoutError: locator.click: Timeout 2000ms exceeded.",
        "  - waiting for locator('#btn-1f9c')",
    ]
    big_gap = _noise(150, prefix="unrelated chatter")
    # same failure 4x with varying ids/numbers — must collapse to one block
    lines = list(big_gap)  # leading gap so every block window has the same shape
    for retry in range(4):
        variant = f"{retry}f9c"
        lines += [
            ln.replace("1f9c", variant).replace("2000", str(2000 + retry)) for ln in block
        ]
        lines += big_gap
    out = filter_log("\n".join(lines), context=10)
    assert out.blocks_kept == 1
    assert out.blocks_dropped_duplicate == 3


def _word(i: int) -> str:
    """Digit-free unique token so duplicate-normalization keeps blocks distinct."""
    return "".join(chr(ord("a") + int(d)) for d in str(i))


def test_caps_output_size():
    # many distinct failures spread far apart -> must stop at the cap
    lines = []
    for i in range(120):
        lines += _noise(90, prefix=f"chatter-{i}-x")
        lines.append(f"Error: unique failure in module {_word(i)}")
    out = filter_log("\n".join(lines), context=40, cap_bytes=15 * 1024)
    assert out.bytes_filtered <= 16 * 1024
    assert out.blocks_dropped_cap > 0
    assert "omitted to stay under the size cap" in out.text


def test_strips_ansi_and_timestamps():
    raw = "2026-06-11T22:13:01.1234567Z \x1b[31mError:\x1b[0m something broke"
    out = filter_log(raw)
    assert "\x1b" not in out.text
    assert "2026-06-11T22:13:01" not in out.text
    assert "Error: something broke" in out.text


def test_no_anchors_returns_tail_with_note():
    out = filter_log("\n".join(_noise(500)))
    assert out.anchor_count == 0
    assert "no failure anchors found" in out.note
    assert out.bytes_filtered <= 4096
    assert "pkg-499" in out.text


def test_real_run_logs_fixture_filters_to_failure():
    zf = zipfile.ZipFile(io.BytesIO((FIXTURES / "gh" / "run_logs.zip").read_bytes()))
    text = "\n".join(
        zf.read(n).decode("utf-8", "replace") for n in sorted(zf.namelist())
    )
    out = filter_log(text)
    assert out.bytes_raw > 1_000_000
    assert out.bytes_filtered <= 16 * 1024
    assert "TimeoutError" in out.text
    assert "flaky-selector.spec.ts" in out.text
