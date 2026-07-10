"""Strategy: raise the failing action/assertion timeout (≤3×, cap 60 s)."""

from __future__ import annotations

import re

from .base import CauseMatchStrategy, PatchOp, register_strategy

FACTOR = 3
CAP_MS = 60_000

# Accept TS numeric separators (idiomatic `25_000`); they are stripped before int().
# `[ \t]` (never `\s`) so a match cannot span lines: this strategy anchors its
# PatchOps to a single containing line, and a multi-line
# `test.setTimeout(\n  60_000\n)` match would have no such line.
_TIMEOUT_RE = re.compile(r"timeout:[ \t]*([\d_]+)")
_SET_TIMEOUT_RE = re.compile(r"test\.setTimeout\([ \t]*([\d_]+)[ \t]*\)")


def _int(token: str) -> int:
    return int(token.replace("_", ""))


def _bumped(value: int) -> int | None:
    new = min(value * FACTOR, CAP_MS)
    return new if new > value else None


class BumpTimeout(CauseMatchStrategy):
    name = "bump_timeout"
    cause = "timeout"

    def plan(self, test_source: str, diagnosis: dict, trace, test_file: str) -> list[PatchOp]:
        selector = self._selector(trace, diagnosis)
        if not isinstance(selector, str):
            selector = None

        # 1) explicit `timeout: N` on the line using the failing selector
        if selector:
            for line in test_source.splitlines():
                if selector in line:
                    m = _TIMEOUT_RE.search(line)
                    if m:
                        return self._replace_on_line(line, m, test_file)

        # 2) a `timeout: N` matching the trace's failing timeout anywhere
        trace_timeout = int(trace.timeout_ms) if trace and trace.timeout_ms else None
        if trace_timeout:
            for line in test_source.splitlines():
                m = _TIMEOUT_RE.search(line)
                if m and _int(m.group(1)) == trace_timeout:
                    return self._replace_on_line(line, m, test_file)

        # 3) whole-test budget via test.setTimeout(N)
        m = _SET_TIMEOUT_RE.search(test_source)
        if m:
            new = _bumped(_int(m.group(1)))
            if new is None:
                return []
            # Defense in depth: decline (never raise StopIteration) if no single
            # line contains the match — an anchored PatchOp needs one.
            line = next((ln for ln in test_source.splitlines() if m.group(0) in ln), None)
            if line is None:
                return []
            return [
                PatchOp(
                    file=test_file,
                    op="replace",
                    anchor=line,
                    old=m.group(0),
                    new=f"test.setTimeout({new})",
                )
            ]
        return []

    @staticmethod
    def _replace_on_line(line: str, m: re.Match, test_file: str) -> list[PatchOp]:
        new = _bumped(_int(m.group(1)))
        if new is None:  # already at/above the cap — bumping cannot help
            return []
        return [
            PatchOp(
                file=test_file,
                op="replace",
                anchor=line,
                old=m.group(0),
                new=f"timeout: {new}",
            )
        ]


register_strategy(BumpTimeout())
