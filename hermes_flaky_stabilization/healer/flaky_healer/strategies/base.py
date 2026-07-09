"""Strategy protocol, PatchOp application and the strategy registry.

A PatchOp is a serializable anchored edit so recipes can store and replay it:
``{"file", "op": "replace"|"insert_before", "anchor", "old", "new"}``.
``anchor`` is a substring that locates the edit site; ``replace`` swaps the
first occurrence of ``old`` at-or-after the anchor, ``insert_before`` inserts
``new`` as a line right before the line containing the anchor.
"""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class StrategyError(RuntimeError):
    pass


@dataclass
class PatchOp:
    file: str
    op: str  # "replace" | "insert_before"
    anchor: str
    new: str
    old: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PatchOp:
        return cls(
            file=data["file"],
            op=data["op"],
            anchor=data["anchor"],
            new=data["new"],
            old=data.get("old"),
        )


@runtime_checkable
class Strategy(Protocol):
    name: str

    def applies(self, diagnosis: dict, trace) -> bool: ...

    def plan(self, test_source: str, diagnosis: dict, trace, test_file: str) -> list[PatchOp]: ...


def _apply_to_text(text: str, op: PatchOp) -> str:
    idx = text.find(op.anchor)
    if idx < 0:
        raise StrategyError(f"anchor not found in {op.file}: {op.anchor!r}")
    if op.op == "replace":
        if not op.old:
            raise StrategyError(f"replace op without 'old' for {op.file}")
        pos = text.find(op.old, idx)
        if pos < 0:  # anchor may sit after the fragment (same line)
            line_start = text.rfind("\n", 0, idx) + 1
            pos = text.find(op.old, line_start)
        if pos < 0:
            raise StrategyError(f"'old' fragment not found near anchor in {op.file}: {op.old!r}")
        return text[:pos] + op.new + text[pos + len(op.old):]
    if op.op == "insert_before":
        line_start = text.rfind("\n", 0, idx) + 1
        insertion = op.new if op.new.endswith("\n") else op.new + "\n"
        return text[:line_start] + insertion + text[line_start:]
    raise StrategyError(f"unknown patch op {op.op!r}")


def _iter_targets(root: Path | str, ops: list[PatchOp]):
    """Group ops by file and resolve each target under root, guarding traversal.

    Yields ``(relpath, target_path, [ops])``. Raises ``StrategyError`` for a
    target that escapes the root or does not exist.
    """
    root = Path(root)
    root_resolved = root.resolve()
    by_file: dict[str, list[PatchOp]] = {}
    for op in ops:
        by_file.setdefault(op.file, []).append(op)
    for rel, file_ops in by_file.items():
        target = root / rel
        try:  # never touch files outside the project root, even for an absolute/.. op.file
            target.resolve().relative_to(root_resolved)
        except ValueError as exc:
            raise StrategyError(f"patch target escapes the project root: {rel!r}") from exc
        if not target.is_file():
            raise StrategyError(f"patch target does not exist: {target}")
        yield rel, target, file_ops


def _changed_text(target: Path, file_ops: list[PatchOp]) -> tuple[str, str]:
    before = target.read_text()
    after = before
    for op in file_ops:
        after = _apply_to_text(after, op)
    return before, after


def compute_changes(root: Path | str, ops: list[PatchOp]) -> dict[str, tuple[str, str]]:
    """{relpath: (before, after)} computed from ``root`` WITHOUT writing anything.

    Lets the diff be generated off the original tree so the burn-in sandbox copy
    is the only project copy made (the sandbox applies the same ops to its copy).
    Runs the identical traversal/existence/anchor validation as ``apply_ops``.
    """
    return {
        rel: _changed_text(target, file_ops)
        for rel, target, file_ops in _iter_targets(root, ops)
    }


def apply_ops(root: Path | str, ops: list[PatchOp]) -> dict[str, tuple[str, str]]:
    """Apply ops to files under root; returns {relpath: (before, after)}."""
    changes: dict[str, tuple[str, str]] = {}
    for rel, target, file_ops in _iter_targets(root, ops):
        before, after = _changed_text(target, file_ops)
        target.write_text(after)
        changes[rel] = (before, after)
    return changes


_NO_NEWLINE = "\n\\ No newline at end of file\n"


def unified_diff(changes: dict[str, tuple[str, str]]) -> str:
    """git-apply-compatible unified diff over the recorded changes.

    difflib omits the ``\\ No newline at end of file`` marker, which makes
    ``git apply`` reject (or corrupt) a patch whenever a context/changed line
    is the final line of a file that lacks a trailing newline. We re-insert the
    marker after any emitted +/- /context line that has no newline — that line
    can only be the last line of its side, and only when the file had no EOF
    newline.
    """
    chunks = []
    for rel, (before, after) in sorted(changes.items()):
        diff_lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        out = []
        for line in diff_lines:
            out.append(line)
            if line[:1] in (" ", "+", "-") and not line.endswith("\n"):
                out.append(_NO_NEWLINE)
        chunks.append("".join(out))
    return "".join(chunks)


REGISTRY: dict[str, Strategy] = {}


def register_strategy(strategy: Strategy) -> Strategy:
    REGISTRY[strategy.name] = strategy
    return strategy


def get_strategy(name: str | None) -> Strategy | None:
    return REGISTRY.get(name or "")


def candidate_strategies(diagnosis: dict, trace) -> list[Strategy]:
    """Applicable strategies, recommended one first (plan §4.3 selection)."""
    ordered: list[Strategy] = []
    recommended = get_strategy((diagnosis or {}).get("recommended_strategy"))
    if recommended is not None and recommended.applies(diagnosis, trace):
        ordered.append(recommended)
    for strategy in REGISTRY.values():
        if strategy in ordered:
            continue
        if strategy.applies(diagnosis, trace):
            ordered.append(strategy)
    return ordered
