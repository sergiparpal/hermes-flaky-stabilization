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
from typing import Any

from . import config, skill_export, strategies
from . import diagnose as diagnose_mod
from .recipes import matcher
from .recipes import signature as signature_mod
from .sandbox.base import RunReport, Sandbox, SandboxError, create_sandbox
from .trace import TraceSummary

_VOLATILE_DIAGNOSIS_KEYS = ("diagnosis_stage", "llm_used", "llm_error")


class _HealAborted(Exception):
    """A heal step failed in a way the caller reports as ``report.error``.

    Raised instead of returning early: ``heal`` stamps ``duration_s`` in one
    ``finally`` block, so no step has to remember to do it (seven separate
    early returns each used to).
    """


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

    def apply_burnin(self, diff: str, burnin: RunReport) -> None:
        """Record a completed burn-in. Both the first-pass and the
        recipe-fallback path land here, so ``stable``/``isolation`` can never be
        derived from the burn-in in one place and forgotten in the other."""
        self.diff = diff
        self.burnin = burnin.to_dict()
        self.stable = burnin.all_green
        self.isolation = burnin.isolation

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
    except SandboxError as exc:  # infra failure: surface a clean tool error
        return "", None, f"sandbox failure during burn-in: {exc}"
    return diff, burnin, None


def _clean_diagnosis(diagnosis: dict) -> dict:
    return {k: v for k, v in diagnosis.items() if k not in _VOLATILE_DIAGNOSIS_KEYS}


def _persist_recipe(store, report: HealReport, diagnosis: dict, parts: dict) -> None:
    """Store the procedure that just went green and refresh the skill mirror.

    Both the first-pass success and the recipe-fallback success persist through
    here, so the ``_clean_diagnosis`` scrub and the ``export_learned_patterns``
    refresh cannot be applied on one path and skipped on the other.
    """
    store.upsert_recipe(
        report.recipe_signature,
        diagnosis=_clean_diagnosis(diagnosis),
        strategy=report.strategy,
        patch_ops=report.ops,
        signature_parts=parts,
    )
    skill_export.export_learned_patterns(store)


def _resolve_spec_path(repo_dir: Path, test_id: str) -> Path:
    """The spec named by *test_id*, refusing to escape *repo_dir*.

    Runs before any sandbox is created, so a traversal attempt never spins one up.
    """
    # test_id must be a relative path that stays inside repo_dir: an absolute or
    # ../-escaping value would otherwise read and patch files outside the project.
    spec_path = repo_dir / test_id
    try:
        spec_path.resolve().relative_to(repo_dir.resolve())
    except (ValueError, OSError) as exc:
        raise _HealAborted(
            f"test_id must be a relative path inside repo_dir: {test_id!r}"
        ) from exc
    if not spec_path.is_file():
        raise _HealAborted(f"test file not found: {spec_path}")
    return spec_path


def _read_spec_text(spec_path: Path) -> str:
    """Explicit utf-8: on LANG=C hosts the locale default is ASCII and a
    non-ASCII spec would raise UnicodeDecodeError out of the tool contract."""
    try:
        return spec_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _HealAborted(f"test file is not valid UTF-8: {spec_path} ({exc})") from exc


def _match_recipe(store, report: HealReport, parts: dict | None,
                  diagnosis: dict | None, strategy_override: str | None):
    """The stored recipe for this signature, or ``None`` to diagnose afresh."""
    if store is None or parts is None or diagnosis is not None or strategy_override:
        return None
    recipe, kind = matcher.match(store, parts)
    if recipe is not None:
        report.recipe_match = kind
    return recipe


def _ops_from_recipe(store, report: HealReport, recipe, test_source: str,
                     trace_summary: TraceSummary | None,
                     test_id: str) -> list[strategies.PatchOp]:
    """Instantiate *recipe* against the CURRENT source; ``[]`` if it is stale."""
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
        return ops
    # Stale recipe: cannot instantiate against the current source.
    store.bump_recipe(recipe.signature, success=False)
    report.recipe_fallback = True
    report.recipe_match = None
    return []


def _ops_from_diagnosis(report: HealReport, session: _HealSession,
                        diagnosis: dict | None,
                        strategy_override: str | None) -> list[strategies.PatchOp]:
    """The full stage-1/2 diagnosis path taken when no recipe applies."""
    if diagnosis is not None:
        report.diagnosis = diagnosis
        report.diagnosis_stage = "given"
    else:
        report.diagnosis = diagnose_mod.diagnose(
            trace=session.trace_summary,
            log_excerpt=session.log_excerpt,
            complete_structured=session.llm.callable,
        )
        report.diagnosis_stage = report.diagnosis.get("diagnosis_stage")
    report.llm_calls = session.llm.calls
    if report.recipe_signature is None:
        report.recipe_signature = signature_mod.signature_hash(
            signature_mod.parts_from_diagnosis(report.diagnosis)
        )
    return _plan_with_candidates(
        report, session.test_source, report.diagnosis,
        session.trace_summary, session.test_id, strategy_override,
    )


@dataclass
class _HealSession:
    """The invariants of one heal run.

    Bundled because the recipe-fallback path needs almost all of them; passing
    them positionally made for an eleven-parameter helper whose call site no
    reviewer could check.
    """

    report: HealReport
    repo_dir: Path
    test_id: str
    test_source: str
    sandbox: Sandbox
    trace_summary: TraceSummary | None
    log_excerpt: str | None
    llm: _CountingLLM
    store: Any  # HealerStore | None — duck-typed, this module never imports it
    parts: dict | None
    reproduce_runs: int
    burnin_runs: int


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
    reproduce_runs: int | None = None,
    burnin_runs: int | None = None,
) -> HealReport:
    """Recipe-or-diagnose → patch a copy → reproduce M times → burn in N times.

    ``duration_s`` is stamped once, in the ``finally``; steps that cannot
    continue raise :class:`_HealAborted` rather than returning early.
    """
    started = time.monotonic()
    repo_dir = Path(repo_dir)
    report = HealReport(test_id=test_id, repo_dir=str(repo_dir))
    try:
        # Order matters: validate the path before a sandbox exists, and publish
        # report.isolation before the source read can abort — both preserved
        # from the pre-decomposition body.
        spec_path = _resolve_spec_path(repo_dir, test_id)
        sandbox = sandbox or create_sandbox()
        report.isolation = sandbox.isolation
        default_reproduce, default_burnin = config.burnin()
        test_source = _read_spec_text(spec_path)

        parts: dict | None = None
        if trace_summary is not None:
            parts = signature_mod.parts_from_trace(trace_summary)
            report.recipe_signature = signature_mod.signature_hash(parts)

        session = _HealSession(
            report=report,
            repo_dir=repo_dir,
            test_id=test_id,
            test_source=test_source,
            sandbox=sandbox,
            trace_summary=trace_summary,
            log_excerpt=log_excerpt,
            llm=_CountingLLM(complete_structured),
            store=store,
            parts=parts,
            reproduce_runs=reproduce_runs or default_reproduce,
            burnin_runs=burnin_runs or default_burnin,
        )
        _run_heal(session, diagnosis=diagnosis, strategy_override=strategy_override)
    except _HealAborted as exc:
        report.error = str(exc)
    finally:
        report.duration_s = time.monotonic() - started
    return report


def _run_heal(session: _HealSession, *, diagnosis: dict | None,
              strategy_override: str | None) -> None:
    """The heal body: resolve ops, reproduce, burn in, record. Mutates the report."""
    report = session.report

    # --- ops: a stored recipe if one instantiates, else a full diagnosis -------
    recipe = _match_recipe(session.store, report, session.parts, diagnosis, strategy_override)
    ops: list[strategies.PatchOp] = []
    if recipe is not None:
        ops = _ops_from_recipe(
            session.store, report, recipe, session.test_source,
            session.trace_summary, session.test_id,
        )
        if not ops:
            recipe = None  # stale; fall through to the diagnosis path
    if recipe is None:
        ops = _ops_from_diagnosis(report, session, diagnosis, strategy_override)
        if not ops:
            return  # no strategy applies: not an error, just nothing to do
    report.ops = [op.to_dict() for op in ops]

    # --- reproduce on the unpatched project (validation is never skipped) -----
    try:
        repro = session.sandbox.run_test(
            session.repo_dir, session.test_id, repeats=session.reproduce_runs
        )
    except SandboxError as exc:  # infra failure: surface a clean tool error
        raise _HealAborted(f"sandbox failure during reproduce runs: {exc}") from exc
    report.repro = repro.to_dict()
    report.reproduced = repro.any_failed
    if not report.reproduced:
        report.confidence_note = (
            f"flake did not reproduce in {session.reproduce_runs} pre-patch runs; "
            "healing anyway with reduced confidence"
        )

    # --- patch + burn-in --------------------------------------------------------
    diff, burnin, error = _patch_and_burnin(
        session.sandbox, session.repo_dir, session.test_id, ops, session.burnin_runs
    )
    if error:
        raise _HealAborted(error)
    report.apply_burnin(diff, burnin)

    # --- recipe bookkeeping ------------------------------------------------------
    if session.store is None:
        return
    if report.recipe_used and recipe is not None:
        session.store.bump_recipe(recipe.signature, success=report.stable)
        if not report.stable:
            _fallback_after_failed_recipe(session)
    elif report.stable and session.parts is not None:
        _persist_recipe(session.store, report, report.diagnosis, session.parts)


def _fallback_after_failed_recipe(session: _HealSession) -> None:
    """Plan §6 edge: failed recipe burn-in → record failure (done by caller)
    and fall back to the full diagnosis path once."""
    report = session.report
    report.recipe_fallback = True
    fresh = diagnose_mod.diagnose(
        trace=session.trace_summary,
        log_excerpt=session.log_excerpt,
        complete_structured=session.llm.callable,
    )
    report.llm_calls = session.llm.calls
    fresh_ops = _plan_with_candidates(
        report, session.test_source, fresh, session.trace_summary, session.test_id, None
    )
    if not fresh_ops or [op.to_dict() for op in fresh_ops] == report.ops:
        report.error = None
        return  # nothing different to try; keep the failed-recipe result visible
    diff, burnin, error = _patch_and_burnin(
        session.sandbox, session.repo_dir, session.test_id, fresh_ops, session.burnin_runs
    )
    if error:
        report.error = error
        return
    report.recipe_used = False
    report.recipe_match = None
    report.diagnosis = fresh
    report.diagnosis_stage = fresh.get("diagnosis_stage")
    report.ops = [op.to_dict() for op in fresh_ops]
    report.apply_burnin(diff, burnin)
    if report.stable and session.parts is not None:
        _persist_recipe(session.store, report, fresh, session.parts)
