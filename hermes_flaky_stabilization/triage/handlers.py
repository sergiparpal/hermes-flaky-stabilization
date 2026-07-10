"""Handler glue for ``triage_pipeline_failure``.

Orchestrates the pipeline:

    validate input
      → logfetch (local path or https URL)
      → prefilter (bounded, signal-dense excerpt)
      → compute signature
      → patterns.lookup (prior becomes a classifier hint)
      → optional in-package history enrichment (guarded, non-fatal)
      → classifier.classify (LLM + heuristic fallback)
      → patterns.record
      → JSON envelope

Kept thin: this module decides nothing about *how* each step works, only the
order and the error contract. It is pure standard library — Hermes objects
(``llm``, ``hermes_home``) are injected by ``register()`` in ``__init__.py``
— so it unit-tests with fakes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import classifier, enrichment, logfetch, patterns, prefilter, redact
from .ports import LlmPort

logger = logging.getLogger(__name__)

# How many tail lines to classify when the log carries no clear failure signal.
_NO_SIGNAL_TAIL_LINES = 60
# Decimal places the confidence is rounded to in the response envelope. Fixed to
# keep the emitted JSON parity-identical with the legacy plugin's precision.
_CONFIDENCE_DECIMALS = 3
# Unified-plugin adaptation (plan D4/Appendix C): patterns live in the
# consolidated state.db (tables triage_patterns/_fts), not the legacy
# cache/ci_triage_patterns.db.
_DB_RELATIVE = ("flaky-stabilization", "state.db")


# --------------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------------

def _error(message: str, remediation: str = "") -> str:
    return json.dumps(
        {"success": False, "error": message, "remediation": remediation},
        ensure_ascii=False,
    )


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _resolve_hermes_home(hermes_home: str | None) -> Path:
    """Resolve the Hermes home directory without importing anything from Hermes.

    The profile-aware resolution (``hermes_constants.get_hermes_home()``) is done
    once in ``__init__.register`` and injected as ``hermes_home``; this keeps the
    orchestrator Hermes-free. The env fallback only serves the direct-call path
    (tests, or a caller that does not inject a home).

    It borrows ``paths.env_home``/``default_home`` rather than the full
    :func:`paths.get_hermes_home` chain: those two are pure stdlib, so the
    ``$HERMES_HOME`` expansion stays shared with every other stage while this
    module keeps its no-Hermes-imports property.
    """
    if hermes_home:
        return Path(hermes_home)
    from .. import paths

    return (paths.env_home() or paths.default_home()).resolve()


def _db_path(hermes_home: str | None) -> Path:
    return _resolve_hermes_home(hermes_home).joinpath(*_DB_RELATIVE)


def _infer_project(args: dict[str, Any]) -> str:
    explicit = args.get("project")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    cwd = (os.environ.get("TERMINAL_CWD") or os.getcwd() or "").strip()
    name = os.path.basename(cwd.rstrip("/")) if cwd else ""
    return name or "default"


def _tail_excerpt(raw: str, n_lines: int = _NO_SIGNAL_TAIL_LINES) -> str:
    """The low-signal fallback: the last *n_lines*, hard-capped by characters.

    A line cap alone is no bound at all — a 20 MB single-line log (CR progress
    bars, minified output) is "one line" and would flow whole through
    redaction, every signature regex, and into the LLM call. Cap by
    :data:`prefilter.DEFAULT_CHAR_CAP`, keeping the TAIL (errors cluster at
    the end), with the standard truncation note.

    The char-cap-and-note step is :func:`prefilter.tail_with_note` — the same
    rule prefilter applies to its own excerpt, shared so the two cannot drift.
    """
    lines = prefilter.strip_ansi(raw).split("\n")
    excerpt = "\n".join(lines[-n_lines:])
    return prefilter.tail_with_note(excerpt, prefilter.DEFAULT_CHAR_CAP)


def _open_and_lookup(
    db_path: Path, project: str, signature: str, excerpt: str
) -> tuple[patterns.PatternStore | None, dict[str, Any] | None]:
    """Open the pattern store and fetch any prior for *signature*.

    Returns ``(store, prior)`` on success — the caller owns the open store and
    must close it. On any failure returns ``(None, None)`` with the connection
    already closed, so the caller's happy path needs only a single
    ``finally``-close around the later record step.
    """
    store: patterns.PatternStore | None = None
    try:
        store = patterns.PatternStore(db_path)
        return store, store.lookup(project, signature, excerpt)
    except Exception:
        logger.warning("pattern store unavailable", exc_info=True)
        if store is not None:
            try:
                store.close()  # don't leak the connection on a lookup error
            except Exception:
                pass
        return None, None


def _build_payload(
    result: dict[str, Any],
    *,
    prior: dict[str, Any] | None,
    prior_occurrences: int,
    project: str,
    signature: str | None,
    stats: dict[str, object],
    low_signal: bool,
    fetch_stats: dict[str, object],
    enrichment_data: Any | None,
    incident_context: Any | None,
) -> dict[str, Any]:
    """Assemble the success-envelope payload from the pipeline's outputs.

    Extracted from the orchestrator's tail so the exact key set, insertion
    order (which fixes the emitted JSON order), and conditional-inclusion rules
    live in one place. Keeping it here — rather than inline in the sequential
    orchestrator — stops the parity-with-the-legacy-plugin envelope from
    drifting when the orchestrator flow is edited.
    """
    payload: dict[str, Any] = {
        "category": result["category"],
        "confidence": round(float(result.get("confidence", 0.0)), _CONFIDENCE_DECIMALS),
        "summary": result.get("summary", ""),
        "evidence": result.get("evidence", []),
        "suggested_action": result.get("suggested_action", ""),
        "classification_method": result.get("method", "llm"),
        "prior_seen": prior is not None,
        "prior_occurrences": prior_occurrences,
        "project": project,
        "signature": signature or "",
        "log_stats": {
            "original_bytes": stats.get("original_bytes", 0),
            "hit_count": stats.get("hit_count", 0),
            "truncated": stats.get("truncated", False),
            "low_signal": low_signal,
        },
    }
    # Only present when a remote body exceeded the fetch cap and the tail was
    # kept (key omitted otherwise — the envelope stays parity-identical with
    # the legacy plugin for untruncated fetches).
    if fetch_stats.get("fetch_truncated"):
        payload["log_stats"]["fetch_truncated"] = True
    if prior is not None and prior.get("fuzzy"):
        payload["prior_match"] = "fuzzy"
    if enrichment_data is not None:
        payload["enrichment"] = enrichment_data
    if incident_context is not None:
        payload["incident_context"] = incident_context
    return payload


# --------------------------------------------------------------------------
# Tool entry point
# --------------------------------------------------------------------------

def triage_pipeline_failure(
    args: dict[str, Any],
    *,
    llm: LlmPort | None = None,
    hermes_home: str | None = None,
    enable_enrichment: bool = True,
    incident_context: Any | None = None,
) -> str:
    """Triage one CI/CD pipeline failure log. Always returns a JSON string."""
    # --- validate (before any side effect) -------------------------------
    if not isinstance(args, dict):
        return _error(
            "Invalid arguments: expected an object.",
            "Call with {\"log_url_or_path\": \"...\"}.",
        )
    target = args.get("log_url_or_path")
    if not isinstance(target, str) or not target.strip():
        return _error(
            "Missing required argument 'log_url_or_path'.",
            "Pass a local CI log file path or an https:// URL to the raw log.",
        )
    project = _infer_project(args)

    # --- retrieve --------------------------------------------------------
    try:
        raw, fetch_stats = logfetch.fetch_with_stats(target)
    except logfetch.LogFetchError as exc:
        return _error(str(exc), getattr(exc, "remediation", ""))
    except Exception as exc:  # defensive — never leak a traceback
        logger.exception("log retrieval failed")
        return _error(
            f"Failed to retrieve log ({type(exc).__name__}).",
            "Check the path/URL and try again.",
        )

    # --- pre-filter ------------------------------------------------------
    excerpt, stats = prefilter.prefilter(raw)
    low_signal = stats.get("hit_count", 0) == 0
    if low_signal:
        excerpt = _tail_excerpt(raw)
        if excerpt.startswith(prefilter.TRUNCATION_NOTE):
            stats["truncated"] = True
    # Scrub secrets BEFORE the excerpt is hashed, stored, sent to the LLM, used
    # to seed enrichment, or echoed back. CI logs routinely contain tokens/keys;
    # everything downstream operates on this redacted form.
    excerpt = redact.redact(excerpt)

    # --- signature + prior ----------------------------------------------
    # The store owns the "degenerate excerpt" rule: compute_signature returns
    # None when the excerpt normalises to nothing (it would otherwise collide
    # across all such logs), so a None signature means skip the store entirely.
    signature = patterns.compute_signature(excerpt)
    store: patterns.PatternStore | None = None
    prior: dict[str, Any] | None = None
    if signature is not None:
        store, prior = _open_and_lookup(
            _db_path(hermes_home), project, signature, excerpt
        )

    # --- optional enrichment (guarded) ----------------------------------
    # Unified-plugin adaptation (plan §2 row 6): enrichment now queries the
    # in-package history stage directly instead of dispatching partner tools
    # with a wrong argument key (the legacy live bug).
    enrichment_data = None
    if enable_enrichment:
        enrichment_data = enrichment.enrich(excerpt)
        if enrichment_data is not None:
            # Another tool's output may also carry secrets — scrub before it is
            # fed to the LLM or echoed in the result. Redaction is the
            # orchestrator's single chokepoint; enrichment.enrich never redacts.
            enrichment_data = redact.redact_obj(enrichment_data)

    # NET-NEW (plan D9): incidents→triage feedback — the orchestrator may seed
    # related incidents from the local index as an extra weak prior. Same
    # redaction chokepoint as enrichment: scrub before the LLM sees or the
    # payload echoes it.
    if incident_context is not None:
        incident_context = redact.redact_obj(incident_context)

    # --- classify --------------------------------------------------------
    result = classifier.classify(
        llm, excerpt, prior=prior, enrichment=enrichment_data,
        incident_context=incident_context,
    )

    # --- record ----------------------------------------------------------
    prior_occurrences = int(prior.get("occurrences", 0)) if prior else 0
    # Don't teach the store low-signal heuristic defaults — they'd resurface as
    # noisy fuzzy priors. A confident LLM call on a low-signal log still learns.
    learn = not (low_signal and result.get("method") == "heuristic")
    if store is not None:
        try:
            if learn:
                store.record(project, signature, result["category"], excerpt)
        except Exception:
            logger.warning("pattern record failed", exc_info=True)
        finally:
            store.close()

    # --- respond ---------------------------------------------------------
    return _ok(_build_payload(
        result,
        prior=prior,
        prior_occurrences=prior_occurrences,
        project=project,
        signature=signature,
        stats=stats,
        low_signal=low_signal,
        fetch_stats=fetch_stats,
        enrichment_data=enrichment_data,
        incident_context=incident_context,
    ))
