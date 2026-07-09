"""Render scan results for humans, for the cron tick, and as JSON.

Kept free of I/O and storage: it takes plain ``Verdict`` objects and metadata and
returns strings, so it is trivially testable and the CLI owns all printing. The
``cron`` renderer returns ``""`` to mean "print nothing" — the no-agent cron
treats empty stdout as a silent tick (the desired behavior on quiet nights).
"""

import json
from dataclasses import asdict

from . import domain


def flaky_changes(prev_keys, new_keys):
    """``(newly_flaky, newly_resolved)`` — sorted test_key lists.

    ``newly_flaky`` = flaky now but not before; ``newly_resolved`` = flaky before
    but not now. Computed from the previous snapshot *before* it is overwritten.
    """
    prev, new = set(prev_keys), set(new_keys)
    return sorted(new - prev), sorted(prev - new)


def _flaky_line(v) -> str:
    return (f"  • {v.test_key}  [{v.fails} fail / {v.passes} pass of {v.runs}]  "
            f"last failure {v.last_failure or '-'}")


def _worst_first(verdicts):
    return sorted(verdicts, key=lambda v: (-v.fails, -v.runs, v.test_key))


def render_human(verdicts, scan_meta) -> str:
    """A readable report of the current flaky / consistently-failing tests."""
    flaky = [v for v in verdicts if v.status == domain.VERDICT_FLAKY]
    cfail = [v for v in verdicts if v.status == domain.VERDICT_CONSISTENTLY_FAILING]

    lines = [
        f"Flaky-test scan — window {scan_meta['window_days']}d, "
        f"min-fails {scan_meta['min_fails']}, include_errors={scan_meta['include_errors']}",
        f"Examined {scan_meta['tests_examined']} test(s): "
        f"{len(flaky)} flaky, {len(cfail)} consistently failing.",
    ]
    if flaky:
        lines += ["", "Flaky tests (intermittent — they both pass and fail in the window):"]
        lines += [_flaky_line(v) for v in _worst_first(flaky)]
    if cfail:
        lines += ["", "Consistently failing (no passes — likely a real break, not flaky):"]
        lines += [f"  • {v.test_key}  [{v.fails} fail of {v.runs}]"
                  for v in _worst_first(cfail)]
    if not flaky and not cfail:
        lines += ["", "No flaky or consistently-failing tests detected."]
    return "\n".join(lines)


def render_cron(report_scope, *, new_flaky_verdicts, newly_flaky, newly_resolved) -> str:
    """The nightly cron message, or ``""`` to deliver nothing (silent tick).

    ``changes-only`` (default): the diff vs the previous scan; empty when nothing
    changed. ``full-set``: the whole current flaky set; empty when there are none.
    """
    if report_scope == "full-set":
        if not new_flaky_verdicts:
            return ""
        lines = [f"Flaky tests currently detected ({len(new_flaky_verdicts)}):"]
        lines += [_flaky_line(v) for v in _worst_first(new_flaky_verdicts)]
        return "\n".join(lines)

    # changes-only
    if not newly_flaky and not newly_resolved:
        return ""
    lines = ["Flaky-test changes since the last scan:"]
    for key in newly_flaky:
        lines.append(f"  + now flaky: {key}")
    for key in newly_resolved:
        lines.append(f"  - no longer flaky: {key}")
    return "\n".join(lines)


def render_json(verdicts, scan_meta, newly_flaky=None, newly_resolved=None) -> str:
    """A machine-readable dump of the scan (for debugging / piping)."""
    return json.dumps(
        {
            "scan": scan_meta,
            "changes": {
                "newly_flaky": newly_flaky or [],
                "newly_resolved": newly_resolved or [],
            },
            "verdicts": [asdict(v) for v in verdicts],
        },
        indent=2,
        ensure_ascii=False,
    )
