"""The deterministic PII gate (plan D6.4).

``assert_evidence_clean(paths)`` wraps ``pii.scanner.scan`` over every
referenced evidence path. Safe ⇔ every scan is ``clean && complete`` (the
scanner's own gate semantics). The gate lives in handlers/pipeline code, not
in a hook — hooks are advisory; this is a hard refusal.

The result never carries raw PII: only the scanner's masked previews are
summarised, and only as type:count aggregates plus file names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..pii import scanner


@dataclass
class GateResult:
    ok: bool
    checked_paths: list[str] = field(default_factory=list)
    findings_summary: dict[str, int] = field(default_factory=dict)
    dirty_paths: list[str] = field(default_factory=list)
    incomplete_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_paths": self.checked_paths,
            "findings_summary": self.findings_summary,
            "dirty_paths": self.dirty_paths,
            "incomplete_paths": self.incomplete_paths,
            "errors": self.errors,
        }


def assert_evidence_clean(paths: list[str] | None) -> GateResult:
    """Scan every path; the gate passes only when ALL are clean AND complete.

    No paths means nothing to gate (ok=True with an empty report) — the caller
    decides whether evidence is mandatory. A scan error fails the gate closed.
    """
    result = GateResult(ok=True)
    for raw in paths or []:
        path = str(raw)
        result.checked_paths.append(path)
        try:
            scan_result = scanner.scan(path, None, None)
        except Exception as exc:  # fail closed — an unscannable path is unsafe
            result.ok = False
            result.errors.append(f"{path}: scan failed ({type(exc).__name__})")
            continue
        if not scan_result.get("success"):
            result.ok = False
            result.errors.append(f"{path}: {scan_result.get('error', 'scan error')}")
            continue
        if not scan_result.get("clean"):
            result.ok = False
            result.dirty_paths.append(path)
            for kind, count in (scan_result.get("summary") or {}).items():
                result.findings_summary[kind] = result.findings_summary.get(kind, 0) + count
        if not scan_result.get("complete"):
            result.ok = False
            result.incomplete_paths.append(path)
    return result
