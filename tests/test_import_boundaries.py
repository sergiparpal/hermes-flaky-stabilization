"""Architecture guard: the module dependency graph must stay layered.

Phase 1 formalized the plugin's boundaries; this test freezes them so a future
import cannot silently re-introduce the edges we removed. The rule is a strict
layering:

    common  ←  stages / storage  ←  orchestrator  ←  registration / cli

* ``common`` is the kernel — it may not import any stage, the orchestrator, or
  storage (nothing above it in the plugin).
* ``storage`` is infrastructure — it may not import a stage or the orchestrator
  (this is what the ``migrate_legacy → healer`` inverted import violated).
* nothing below the orchestrator may import *up* into it (this is what the
  ``incidents/write → orchestrator.gate`` cycle violated).
* the orchestrator composes stages through their PUBLIC ports — it may not
  import a stage's underscore-private name.

The check parses imports statically (no importing of Hermes-facing modules).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = "hermes_flaky_stabilization"
PKG_ROOT = Path(__file__).resolve().parent.parent / PKG

STAGES = frozenset({
    "bugreport", "detective", "healer", "history", "incidents", "pii", "triage",
})


def _package_of(path: Path) -> str:
    """The dotted package that a source file's relative imports resolve against."""
    rel = path.relative_to(PKG_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]  # a module's relative imports resolve against its package
    return ".".join(parts)


def _resolve(pkg: str, level: int, module: str | None) -> str:
    """Resolve one (level, module) relative-import spec to an absolute dotted name."""
    if level == 0:
        return module or ""
    parts = pkg.split(".")
    base = parts[: len(parts) - (level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _imports(path: Path):
    """Yield ``(absolute_target, imported_names)`` for every import in *path*."""
    pkg = _package_of(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, []
        elif isinstance(node, ast.ImportFrom):
            target = _resolve(pkg, node.level or 0, node.module)
            yield target, [a.name for a in node.names]


def _all_modules():
    return sorted(p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _subpkg(path: Path) -> str:
    """The top-level subpackage a file belongs to (e.g. 'incidents'), or '' for
    a root-level module like cli.py / registration.py / paths.py."""
    rel = path.relative_to(PKG_ROOT)
    return rel.parts[0] if len(rel.parts) > 1 else ""


def _plugin_targets(path: Path):
    """Absolute in-plugin import targets of *path* (skips stdlib/third-party)."""
    for target, names in _imports(path):
        if target == PKG or target.startswith(PKG + "."):
            yield target, names


def test_common_kernel_imports_nothing_above_it():
    """common/ may import stdlib only — no stage, orchestrator, or storage."""
    offenders = []
    for path in _all_modules():
        if _subpkg(path) != "common":
            continue
        for target, _ in _plugin_targets(path):
            if not target.startswith(f"{PKG}.common"):
                offenders.append(f"{path.name}: imports {target}")
    assert not offenders, "common/ must depend on nothing above it:\n" + "\n".join(offenders)


def test_storage_infra_imports_no_stage_or_orchestrator():
    """storage/ is infrastructure; it must not reach into a stage or the
    orchestrator (the migrate_legacy → healer inverted import)."""
    forbidden = {f"{PKG}.{s}" for s in STAGES} | {f"{PKG}.orchestrator"}
    offenders = []
    for path in _all_modules():
        if _subpkg(path) != "storage":
            continue
        for target, _ in _plugin_targets(path):
            head = ".".join(target.split(".")[:2])
            if head in forbidden:
                offenders.append(f"{path.name}: imports {target}")
    assert not offenders, "storage/ must not import a stage/orchestrator:\n" + "\n".join(offenders)


def test_nothing_below_imports_up_into_the_orchestrator():
    """No stage or infra module may import the orchestrator (upward edge / cycle,
    e.g. incidents/write → orchestrator.gate). Only the composition root and the
    orchestrator itself may reference it."""
    allowed = {"orchestrator", ""}  # orchestrator internals + root (registration/cli)
    offenders = []
    for path in _all_modules():
        sub = _subpkg(path)
        if sub in allowed:
            continue
        for target, _ in _plugin_targets(path):
            if target == f"{PKG}.orchestrator" or target.startswith(f"{PKG}.orchestrator."):
                offenders.append(f"{sub}/{path.name}: imports {target}")
    assert not offenders, "nothing below may import the orchestrator:\n" + "\n".join(offenders)


def test_orchestrator_uses_stage_public_ports_not_privates():
    """The orchestrator must reach stages through their public surface — never a
    ``_``-prefixed name (the _handle_is_flaky / _resolve_home reaches)."""
    stage_pkgs = {f"{PKG}.{s}" for s in STAGES}
    offenders = []
    for path in _all_modules():
        if _subpkg(path) != "orchestrator":
            continue
        for target, names in _plugin_targets(path):
            head = ".".join(target.split(".")[:2])
            if head not in stage_pkgs:
                continue
            for name in names:
                if name.startswith("_"):
                    offenders.append(f"{path.name}: imports private {target}.{name}")
    assert not offenders, (
        "orchestrator must use stage public ports:\n" + "\n".join(offenders))


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
