"""Strategy: wait for the failing target to become visible before acting."""

from __future__ import annotations

import json
import re

from .base import CauseMatchStrategy, PatchOp, register_strategy

_PAGE_VAR_RE = re.compile(r"\b(\w+)\.(?:locator|getBy\w+)\(")
_EXPECT_TIMEOUT_RE = re.compile(r"timeout:[ \t]*([\d_]+)")

# A line we may safely insert a statement before: it must itself start a
# statement. Anything else (a Prettier-wrapped continuation like
# `  page.locator('#btn')` inside `await expect(\n...\n).toBeVisible();`)
# would get the insertion mid-expression, producing syntactically corrupt
# test code that burn-in can never pass.
_STMT_START_RE = re.compile(r"^(?:await|expect|const|let|var|return)\b")


def _statement_anchor(lines: list[str], idx: int) -> int | None:
    """Index of the line starting the statement that encloses ``lines[idx]``.

    ``idx`` itself when it already begins a statement; otherwise scan backwards
    to the enclosing statement boundary — the previous line ending in ``;``,
    ``{`` or ``}``, or a blank line (top-of-file counts too) — and anchor at
    the line just after it, provided that line plausibly starts a statement.
    Returns ``None`` when no safe anchor exists (the caller must decline).
    """
    if _STMT_START_RE.match(lines[idx].lstrip()):
        return idx
    candidate = 0  # top of file is a boundary when no other is found
    for j in range(idx - 1, -1, -1):
        stripped = lines[j].strip()
        if not stripped or stripped.endswith((";", "{", "}")):
            candidate = j + 1
            break
    if candidate <= idx and _STMT_START_RE.match(lines[candidate].lstrip()):
        return candidate
    return None


class AwaitState(CauseMatchStrategy):
    name = "await_state"
    cause = "race_condition"

    def plan(self, test_source: str, diagnosis: dict, trace, test_file: str) -> list[PatchOp]:
        selector = self._selector(trace, diagnosis)
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
        # Playwright assertions are already condition-aware and retry until
        # their timeout. Extending an explicitly tiny assertion budget is more
        # precise than waiting for the discouraged global `networkidle` state.
        timeout_match = _EXPECT_TIMEOUT_RE.search(lines[target_idx])
        if timeout_match and "expect(" in lines[target_idx]:
            current = int(timeout_match.group(1).replace("_", ""))
            enlarged = 5_000
            if enlarged > current:
                return [PatchOp(
                    file=test_file,
                    op="replace",
                    anchor=lines[target_idx],
                    old=timeout_match.group(0),
                    new=f"timeout: {enlarged}",
                )]
            return []
        # The selector line may sit mid-expression; only ever insert before a
        # plausible statement start, else decline rather than corrupt the test.
        anchor_idx = _statement_anchor(lines, target_idx)
        if anchor_idx is None:
            return []
        anchor_line = lines[anchor_idx]
        if anchor_idx > 0 and ".waitFor({ state: 'visible' })" in lines[anchor_idx - 1]:
            return []  # already patched — keep the op idempotent
        if any(".waitFor({ state: 'visible' })" in lines[i]
               for i in range(anchor_idx, target_idx + 1)):
            return []
        m = _PAGE_VAR_RE.search(lines[target_idx])
        page_var = m.group(1) if m else "page"
        indent = anchor_line[: len(anchor_line) - len(anchor_line.lstrip())]
        return [
            PatchOp(
                file=test_file,
                op="insert_before",
                anchor=anchor_line,
                new=(f"{indent}await {page_var}.locator({json.dumps(selector)})"
                     ".waitFor({ state: 'visible' });"),
            )
        ]


register_strategy(AwaitState())
