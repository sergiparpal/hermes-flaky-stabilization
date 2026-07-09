"""Heal orchestrator: recipe-or-diagnose → patch a copy → reproduce/burn-in.

Burn-in contract (plan §4.4): reproduce the flake M times pre-patch (≥1
failure expected; 0 failures lowers confidence but does NOT abort), then run
N times post-patch and require N/N green for ``stable: true``.

Recipe contract (plan §4.6): a signature hit skips stage-1/2 diagnosis and
strategy selection, instantiates the stored procedure against the CURRENT
test source, and still runs the FULL reproduce + burn-in. A recipe whose
patch no longer applies or whose burn-in fails records a failure and falls
back to the full diagnosis path once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, skill_export, strategies
from . import diagnose as diagnose_mod
from .recipes import matcher
from .recipes import signature as signature_mod
from .sandbox.base import RunReport, Sandbox, create_sandbox
from .trace import TraceSummary

_VOLATILE_DIAGNOSIS_KEYS = ("diagnosis_stage", "llm_used", "llm_error")


@dataclass
class HealReport:
    test_id: str
    repo_dir: str
    diagnosis: dict | None = None
    diagnosis_stage: str | None = None  # recipe | heuristic | llm | given
    strategy: str | None = None
    ops: list[dict] = field(default_factory=list)
    reproduced: bool | None = None
    repro: dict | None = None
    burnin: dict | None = None
    stable: bool = False
    diff: str = ""
    isolation: str | None = None
    llm_calls: int = 0
    recipe_used: bool = False
    recipe_match: str | None = None  # exact | relaxed
    recipe_signature: str | None = None
    recipe_fallback: bool = False
    confidence_note: str = ""
    error: str | None = None
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "repo_dir": self.repo_dir,
            "diagnosis": self.diagnosis,
            "diagnosis_stage": self.diagnosis_stage,
            "strategy": self.strategy,
            "ops": self.ops,
            "reproduced": self.reproduced,
            "repro": self.repro,
            "burnin": self.burnin,
            "stable": self.stable,
            "diff": self.diff,
            "isolation": self.isolation,
            "llm_calls": self.llm_calls,
            "recipe_used": self.recipe_used,
            "recipe_match": self.recipe_match,
            "recipe_signature": self.recipe_signature,
            "recipe_fallback": self.recipe_fallback,
            "confidence_note": self.confidence_note,
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
        }


class _CountingLLM:
    def __init__(self, complete_structured):
        self._fn = complete_structured
        self.calls = 0

    @property
    def callable(self):
        if self._fn is None:
            return None

        def wrapped(**kwargs):
            self.calls += 1
            return self._fn(**kwargs)

        return wrapped


def _plan_with_candidates(
    report: HealReport,
    test_source: str,
    diagnosis: dict,
    trace: TraceSummary | None,
    test_id: str,
    strategy_override: str | None,
) -> list[strategies.PatchOp]:
    if strategy_override:
        strategy = strategies.get_strategy(strategy_override)
        if strategy is None:
            report.error = f"unknown strategy override {strategy_override!r}"
            return []
        candidates = [strategy]
    else:
        candidates = strategies.candidate_strategies(diagnosis, trace)
        if not candidates:
            report.error = (
                f"no fix strategy applies to cause {diagnosis.get('cause')!r} "
                f"(triage: {diagnosis.get('triage_label')!r})"
            )
            return []
    for strategy in candidates:
        ops = strategy.plan(test_source, diagnosis, trace, test_id)
        if ops:
            report.strategy = strategy.name
            report.error = None
            return ops
    report.error = "no candidate strategy produced a patch for this test source"
    return []


def _patch_and_burnin(
    sandbox: Sandbox,
    repo_dir: Path,
    test_id: str,
    ops: list[strategies.PatchOp],
    n: int,
) -> tuple[str, RunReport | None, str | None]:
    """Diff the ops off the original tree (no copy) and burn them in.

    The sandbox makes the single project copy and applies the ops to it via the
    ``prepare`` hook, so the patched burn-in needs no separate scratch copy.
    → (diff, report, error).
    """
    try:
        changes = strategies.compute_changes(repo_dir, ops)
    except strategies.StrategyError as exc:
        return "", None, f"patch application failed: {exc}"
    diff = strategies.unified_diff(changes)
    try:
        burnin = sandbox.run_test(
            repo_dir,
            test_id,
            repeats=n,
            prepare=lambda work: strategies.apply_ops(work, ops),
        )
    except strategies.StrategyError as exc:  # a faithful copy can't diverge, but stay safe
        return "", None, f"patch application failed: {exc}"
    return diff, burnin, None


def _clean_diagnosis(diagnosis: dict) -> dict:
    return {k: v for k, v in diagnosis.items() if k not in _VOLATILE_DIAGNOSIS_KEYS}


def heal(
    *,
    repo_dir: str | Path,
    test_id: str,
    trace_summary: TraceSummary | None = None,
    log_excerpt: str | None = None,
    diagnosis: dict | None = None,
    strategy_override: str | None = None,
    sandbox: Sandbox | None = None,
    complete_structured=None,
    store=None,
    m: int | None = None,
    n: int | None = None,
) -> HealReport:
    started = time.monotonic()
    repo_dir = Path(repo_dir)
    report = HealReport(test_id=test_id, repo_dir=str(repo_dir))

    # test_id must be a relative path that stays inside repo_dir: an absolute or
    # ../-escaping value would otherwise read and patch files outside the project.
    spec_path = repo_dir / test_id
    try:
        spec_path.resolve().relative_to(repo_dir.resolve())
    except (ValueError, OSError):
        report.error = f"test_id must be a relative path inside repo_dir: {test_id!r}"
        report.duration_s = time.monotonic() - started
        return report
    if not spec_path.is_file():
        report.error = f"test file not found: {spec_path}"
        report.duration_s = time.monotonic() - started
        return report

    sandbox = sandbox or create_sandbox()
    report.isolation = sandbox.isolation
    default_m, default_n = config.burnin()
    m = m or default_m
    n = n or default_n
    llm = _CountingLLM(complete_structured)
    test_source = spec_path.read_text()

    # --- signature + recipe short-circuit ------------------------------------
    parts: dict | None = None
    if trace_summary is not None:
        parts = signature_mod.parts_from_trace(trace_summary)
        report.recipe_signature = signature_mod.signature_hash(parts)

    recipe = None
    if store is not None and parts is not None and diagnosis is None and not strategy_override:
        recipe, kind = matcher.match(store, parts)
        if recipe is not None:
            report.recipe_match = kind

    ops: list[strategies.PatchOp] = []
    if recipe is not None:
        strategy = strategies.get_strategy(recipe.strategy)
        ops = (
            strategy.plan(test_source, recipe.diagnosis, trace_summary, test_id)
            if strategy is not None
            else []
        )
        if ops:
            report.recipe_used = True
            report.diagnosis = recipe.diagnosis
            report.diagnosis_stage = "recipe"
            report.strategy = recipe.strategy
        else:  # stale recipe: cannot instantiate against the current source
            store.bump_recipe(recipe.signature, success=False)
            report.recipe_fallback = True
            report.recipe_match = None
            recipe = None

    # --- full diagnosis path ---------------------------------------------------
    if recipe is None:
        if diagnosis is not None:
            report.diagnosis = diagnosis
            report.diagnosis_stage = "given"
        else:
            report.diagnosis = diagnose_mod.diagnose(
                trace=trace_summary,
                log_excerpt=log_excerpt,
                complete_structured=llm.callable,
            )
            report.diagnosis_stage = report.diagnosis.get("diagnosis_stage")
        report.llm_calls = llm.calls
        if report.recipe_signature is None:
            report.recipe_signature = signature_mod.signature_hash(
                signature_mod.parts_from_diagnosis(report.diagnosis)
            )
        ops = _plan_with_candidates(
            report, test_source, report.diagnosis, trace_summary, test_id, strategy_override
        )
        if not ops:
            report.duration_s = time.monotonic() - started
            return report
    report.ops = [op.to_dict() for op in ops]

    # --- reproduce on the unpatched project (validation is never skipped) -----
    repro = sandbox.run_test(repo_dir, test_id, repeats=m)
    report.repro = repro.to_dict()
    report.reproduced = repro.any_failed
    if not report.reproduced:
        report.confidence_note = (
            f"flake did not reproduce in {m} pre-patch runs; healing anyway with "
            "reduced confidence"
        )

    # --- patch + burn-in --------------------------------------------------------
    diff, burnin, error = _patch_and_burnin(sandbox, repo_dir, test_id, ops, n)
    if error:
        report.error = error
        report.duration_s = time.monotonic() - started
        return report
    report.diff = diff
    report.burnin = burnin.to_dict()
    report.stable = burnin.all_green
    report.isolation = burnin.isolation

    # --- recipe bookkeeping ------------------------------------------------------
    if store is not None:
        if report.recipe_used and recipe is not None:
            store.bump_recipe(recipe.signature, success=report.stable)
            if not report.stable:
                _fallback_after_failed_recipe(
                    report, sandbox, repo_dir, test_id, test_source,
                    trace_summary, log_excerpt, llm, store, parts, n,
                )
        elif report.stable and parts is not None:
            store.upsert_recipe(
                report.recipe_signature,
                diagnosis=_clean_diagnosis(report.diagnosis),
                strategy=report.strategy,
                patch_ops=report.ops,
                signature_parts=parts,
            )
            skill_export.export_learned_patterns(store)

    report.duration_s = time.monotonic() - started
    return report


def _fallback_after_failed_recipe(
    report: HealReport,
    sandbox: Sandbox,
    repo_dir: Path,
    test_id: str,
    test_source: str,
    trace_summary: TraceSummary | None,
    log_excerpt: str | None,
    llm: _CountingLLM,
    store,
    parts: dict | None,
    n: int,
) -> None:
    """Plan §6 edge: failed recipe burn-in → record failure (done by caller)
    and fall back to the full diagnosis path once."""
    report.recipe_fallback = True
    fresh = diagnose_mod.diagnose(
        trace=trace_summary, log_excerpt=log_excerpt, complete_structured=llm.callable
    )
    report.llm_calls = llm.calls
    fresh_ops = _plan_with_candidates(report, test_source, fresh, trace_summary, test_id, None)
    if not fresh_ops or [op.to_dict() for op in fresh_ops] == report.ops:
        report.error = None
        return  # nothing different to try; keep the failed-recipe result visible
    diff, burnin, error = _patch_and_burnin(sandbox, repo_dir, test_id, fresh_ops, n)
    if error:
        report.error = error
        return
    report.recipe_used = False
    report.recipe_match = None
    report.diagnosis = fresh
    report.diagnosis_stage = fresh.get("diagnosis_stage")
    report.ops = [op.to_dict() for op in fresh_ops]
    report.diff = diff
    report.burnin = burnin.to_dict()
    report.stable = burnin.all_green
    report.isolation = burnin.isolation
    if report.stable and parts is not None:
        store.upsert_recipe(
            report.recipe_signature,
            diagnosis=_clean_diagnosis(fresh),
            strategy=report.strategy,
            patch_ops=report.ops,
            signature_parts=parts,
        )
        skill_export.export_learned_patterns(store)
