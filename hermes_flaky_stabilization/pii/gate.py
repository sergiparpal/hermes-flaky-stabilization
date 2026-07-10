"""The deterministic PII gate (plan D6.4).

``assert_evidence_clean(paths)`` wraps ``pii.scanner.scan`` over every
referenced evidence path. Safe ⇔ every scan is ``clean && complete`` (the
scanner's own gate semantics). The gate lives in handlers/pipeline code, not
in a hook — hooks are advisory; this is a hard refusal.

Lives in the ``pii`` package (it is a PII concern over ``pii.scanner``), so both
the orchestrator's bug branch and the incidents write handler depend on it
here — neither reaches up into the orchestrator for it, which used to create an
incidents → orchestrator import cycle.

The result never carries raw PII: only the scanner's masked previews are
summarised, and only as type:count aggregates plus file names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import scanner


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


def _configured_max_files() -> int | None:
    """The unified config's ``pii.default_max_files``, sanitised for the scan.

    Read via ``config.load_config`` (no dedicated env var exists for this key,
    so the file value — merged over DEFAULTS — is authoritative). ``None``
    (scanner default) when unset or unusable; clamped to the scanner's hard
    ceiling so a generous config value cannot turn every scan into a
    validation error (which would fail the gate on clean evidence).
    """
    try:
        from .. import config as unified_config

        value = (unified_config.load_config().get("pii") or {}).get("default_max_files")
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return min(value, scanner.MAX_MAX_FILES)


def assert_evidence_clean(paths: list[str] | None,
                          max_files: int | None = None) -> GateResult:
    """Scan every path; the gate passes only when ALL are clean AND complete.

    No paths means nothing to gate (ok=True with an empty report) — the caller
    decides whether evidence is mandatory. A scan error fails the gate closed.
    ``max_files`` defaults to the unified config's ``pii.default_max_files``
    (the scanner's own default when that is unset).
    """
    result = GateResult(ok=True)
    if max_files is None and paths:
        max_files = _configured_max_files()
    for raw in paths or []:
        path = str(raw)
        result.checked_paths.append(path)
        try:
            scan_result = scanner.scan(path, None, max_files)
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
