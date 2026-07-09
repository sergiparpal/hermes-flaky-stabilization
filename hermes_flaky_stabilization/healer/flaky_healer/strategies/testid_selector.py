"""Strategy: replace a fragile selector with getByTestId(...).

Only applicable when the trace's DOM snapshot PROVES the target element (exact
or shape-matched id) carries a data-testid — that proof is computed by the
trace parser into ``trace.testid_on_target``.
"""

from __future__ import annotations

import re

from .base import PatchOp, register_strategy

# A data-testid is attacker-controlled data lifted from the trace's DOM snapshot
# and would be written *verbatim* into a .spec source line. Without this guard a
# value like  x'); require('child_process').execSync('...'); ('  breaks out of the
# string literal and injects arbitrary JS that then runs during burn-in (and, in
# mode=pr, lands in a committed PR). Restrict it to a conservative identifier set
# (no quotes, parens, backslash, semicolons, newlines) so it cannot escape the
# getByTestId('...') literal. Real test ids comfortably fit this charset.
_SAFE_TESTID_RE = re.compile(r"^[\w][\w .:/-]{0,127}$")


def _is_safe_testid(value: object) -> bool:
    return isinstance(value, str) and _SAFE_TESTID_RE.match(value) is not None


class TestidSelector:
    name = "testid_selector"

    def applies(self, diagnosis: dict, trace) -> bool:
        diagnosis = diagnosis or {}
        wants = (
            diagnosis.get("cause") == "fragile_selector"
            or diagnosis.get("recommended_strategy") == self.name
        )
        return wants and trace is not None and bool(trace.testid_on_target)

    def plan(self, test_source: str, diagnosis: dict, trace, test_file: str) -> list[PatchOp]:
        if trace is None or not trace.testid_on_target or not trace.selector:
            return []
        selector, testid = trace.selector, trace.testid_on_target
        # Refuse to synthesize a patch from an unsafe testid rather than inject it.
        if not _is_safe_testid(testid):
            return []
        ops: list[PatchOp] = []
        for quote in ("'", '"'):
            fragile = f".locator({quote}{selector}{quote})"
            replacement = f".getByTestId({quote}{testid}{quote})"
            for line in test_source.splitlines():
                if fragile in line:
                    ops.append(
                        PatchOp(
                            file=test_file,
                            op="replace",
                            anchor=line,
                            old=fragile,
                            new=replacement,
                        )
                    )
        return ops


register_strategy(TestidSelector())
