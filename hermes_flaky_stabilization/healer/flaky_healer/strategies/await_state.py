"""Strategy: wait for network state before the failing action.

Inserts ``await page.waitForLoadState('networkidle')`` right before the
failing assertion/action. Level-triggered (returns immediately when the
network is already idle), so it is safe even when the pending response has
landed by the time the line executes — unlike waitForResponse, which only
observes future responses.
"""

from __future__ import annotations

import re

from .base import PatchOp, register_strategy

_PAGE_VAR_RE = re.compile(r"\b(\w+)\.(?:locator|getBy\w+)\(")


class AwaitState:
    name = "await_state"

    def applies(self, diagnosis: dict, trace) -> bool:
        diagnosis = diagnosis or {}
        return (
            diagnosis.get("cause") == "race_condition"
            or diagnosis.get("recommended_strategy") == self.name
        )

    def plan(self, test_source: str, diagnosis: dict, trace, test_file: str) -> list[PatchOp]:
        selector = (trace.selector if trace else None) or (diagnosis or {}).get("selector")
        if not isinstance(selector, str) or not selector:
            return []
        lines = test_source.splitlines()
        target_idx = None
        for i, line in enumerate(lines):
            if selector in line and "expect(" in line:
                target_idx = i
                break
        if target_idx is None:
            for i, line in enumerate(lines):
                if selector in line:
                    target_idx = i
                    break
        if target_idx is None:
            return []
        target = lines[target_idx]
        if target_idx > 0 and "waitForLoadState" in lines[target_idx - 1]:
            return []  # already patched — keep the op idempotent
        if "waitForLoadState" in target:
            return []
        m = _PAGE_VAR_RE.search(target)
        page_var = m.group(1) if m else "page"
        indent = target[: len(target) - len(target.lstrip())]
        return [
            PatchOp(
                file=test_file,
                op="insert_before",
                anchor=target,
                new=f"{indent}await {page_var}.waitForLoadState('networkidle');",
            )
        ]


register_strategy(AwaitState())
