"""Thin tool handlers. Every handler returns a JSON-encoded string (plan §3.3).

Handlers accept ``(params, **kwargs)``; ``register()`` binds ``ctx`` via
functools.partial. Tests inject doubles through the same keyword surface
(``transport=``, ``sandbox=``, ``store=``) — the host never sets those.
"""

from __future__ import annotations

import functools
import io
import json
import logging
import zipfile
import zlib

try:
    from .flaky_healer import (
        config,
        diagnose,
        gitflow,
        healer,
        logfilter,
        trace,
        zipsafe,
    )
    from .flaky_healer import (
        redact as redact_mod,
    )
    from .flaky_healer.ci import base as ci_base
    from .flaky_healer.ci import github_actions
    from .flaky_healer.recipes import signature as signature_mod
    from .flaky_healer.recipes.store import HealerStore
except ImportError:  # flat import context (tests, path-loaded module)
    from flaky_healer import (
        config,
        diagnose,
        gitflow,
        healer,
        logfilter,
        trace,
        zipsafe,
    )
    from flaky_healer import (
        redact as redact_mod,
    )
    from flaky_healer.ci import base as ci_base
    from flaky_healer.ci import github_actions
    from flaky_healer.recipes import signature as signature_mod
    from flaky_healer.recipes.store import HealerStore

log = logging.getLogger("hermes-flaky-healer")


def _params(params) -> dict:
    if isinstance(params, dict):
        return params
    if isinstance(params, str) and params.strip():
        try:
            decoded = json.loads(params)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _err(message: str, **extra) -> str:
    return json.dumps({"error": message, **extra})


def _json_safe(fn):
    """Tool-handler contract guard: any uncaught exception becomes an error JSON
    string rather than propagating (the host expects every handler to return one).
    """

    @functools.wraps(fn)
    def wrapper(params=None, **kwargs) -> str:
        try:
            return fn(params, **kwargs)
        except Exception as exc:  # noqa: BLE001 — last line of defense for the JSON contract
            log.debug("handler %s failed", fn.__name__, exc_info=True)
            return _err(f"{type(exc).__name__}: {exc}")

    return wrapper


# A GitHub Actions job/step with any of these conclusions is NOT a failure; the
# CI-log fetch keeps only jobs and steps whose conclusion is outside this set.
_NON_FAILURE_CONCLUSIONS = (None, "success", "skipped", "neutral")

# zip entry reads can raise zlib.error/RuntimeError on corrupt/encrypted members,
# which the BadZipFile guard alone does not cover; ZipLimitError guards bombs.
_ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    RuntimeError,
    OSError,
    zipsafe.ZipLimitError,
)


def _scrub_ci_secrets(text: str) -> str:
    """Redact credentials from CI-log text before it reaches the model.

    ``logfilter.filter_log`` is a failure-anchor prefilter, not a redactor, and
    CI logs cluster secrets (``set -x``, env dumps, ``curl -H "Authorization:
    …"``) exactly at/around the failures it keeps. The triage pipeline redacts
    log excerpts at its chokepoint; the healer's ``fetch_ci_logs`` /
    ``heal_flaky_test`` diagnosis paths must do the same rather than hand raw
    secrets to the LLM. Prefer the shared triage-grade CI-log redactor; fall
    back to the local token-shape scrub if it cannot be imported."""
    if not text:
        return text
    try:
        from ..triage.redact import redact as _redact_secrets
    except Exception:  # pragma: no cover - flat/path-loaded import fallback
        try:
            from triage.redact import redact as _redact_secrets
        except Exception:
            return redact_mod.mask_tokens(text)
    return _redact_secrets(text)


_STORE_CACHE: dict[str, HealerStore] = {}


def _store_for(ctx=None) -> HealerStore:
    """Resolve the audit/recipe store, memoized per resolved db path.

    Approval-audit hooks fire on every dispatched write in PR mode; rebuilding a
    HealerStore (which re-opens a connection to probe the migration version) per
    event is wasteful. A HealerStore holds no open connection — connections are
    per-operation — so one shared instance per db path is safe across calls.
    """
    # Unified-plugin adaptation (plan D4/Appendix C): healer tables live in the
    # consolidated state.db (healer_runs/recipes/audit), not a private healer.db.
    db_path = config.data_dir(ctx) / "state.db"
    key = str(db_path)
    store = _STORE_CACHE.get(key)
    if store is None:
        store = HealerStore(db_path)
        _STORE_CACHE[key] = store
    return store


# --------------------------------------------------------------------------
# fetch_ci_logs
# --------------------------------------------------------------------------


def _logs_zip_to_text(blob: bytes, failed_job_names: set[str] | None = None) -> str:
    """Concatenate the .txt entries of a GH run-logs zip, one header per entry.

    When failed job names are known, only their per-job folders are read; if
    that filter matches nothing (layout drift), fall back to every entry.
    """
    zf = zipfile.ZipFile(io.BytesIO(blob))
    # The run-logs archive is only semi-trusted (a malicious workflow controls
    # its contents/size); bound entry count and decompressed bytes like traces.
    zipsafe.guard_entry_count(zf)
    budget = zipsafe.ZipBudget()
    names = [n for n in zf.namelist() if n.endswith(".txt")]

    def folder(name: str) -> str:
        return name.split("/", 1)[0] if "/" in name else ""

    selected = names
    if failed_job_names:
        filtered = [n for n in names if folder(n) in failed_job_names]
        if filtered:
            selected = filtered
    parts = []
    for name in sorted(selected):
        body = budget.read(zf, name).decode("utf-8", "replace")
        parts.append(f"===== {name} =====\n{body}")
    return "\n".join(parts)


def _make_ci_adapter(transport):
    """Build the GitHub Actions CI adapter shared by the two CI-log call sites.

    Returns ``(adapter, has_credentials)``. ``has_credentials`` is False ONLY in
    the "no GITHUB_TOKEN and no injected transport" case — the sole case the
    adapter cannot be built to reach the API, so ``adapter`` is then ``None``.
    The two callers diverge on that signal (``fetch_ci_logs`` returns an error
    envelope, ``_pull_ci_log`` degrades to a warning), so the helper only
    centralizes the identical token guard, the ``"injected-transport"`` token
    fallback and the ``api_base`` wiring, keeping the two sites from drifting.
    """
    token = config.github_token()
    if not token and transport is None:
        return None, False
    adapter = github_actions.GitHubActionsCI(
        token=token or "injected-transport",
        transport=transport,
        api_base=config.github_api_base(),
    )
    return adapter, True


@_json_safe
def fetch_ci_logs(params, ctx=None, transport=None, **kwargs) -> str:
    p = _params(params)
    build_id = str(p.get("build_id") or "").strip()
    repo = str(p.get("repo") or "").strip()
    if not build_id or not repo:
        return _err("both 'build_id' and 'repo' (owner/name) are required")

    adapter, has_credentials = _make_ci_adapter(transport)
    if not has_credentials:
        return _err(
            "GITHUB_TOKEN is not set; fetch_ci_logs needs it to call the GitHub API",
            hint="export GITHUB_TOKEN with actions:read scope",
        )
    try:
        run = adapter.get_run(repo, build_id)
        jobs = adapter.get_jobs(repo, build_id)
        blob = adapter.download_logs_zip(repo, build_id)
    except ci_base.CIError as exc:
        return _err(f"GitHub API error: {exc}", status=exc.status)

    failed_jobs = []
    for job in jobs.get("jobs", []):
        if job.get("conclusion") in _NON_FAILURE_CONCLUSIONS:
            continue
        failed_jobs.append(
            {
                "id": job.get("id"),
                "name": job.get("name"),
                "conclusion": job.get("conclusion"),
                "failed_steps": [
                    s.get("name")
                    for s in job.get("steps", [])
                    if s.get("conclusion") not in _NON_FAILURE_CONCLUSIONS
                ],
            }
        )

    try:
        text = _logs_zip_to_text(blob, {j["name"] for j in failed_jobs if j.get("name")})
    except _ZIP_READ_ERRORS:
        return _err("run logs archive is not a valid zip file (corrupt or unreadable)")

    filtered = logfilter.filter_log(text)
    return json.dumps(
        {
            "run": {
                "id": run.get("id"),
                "name": run.get("name"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "head_branch": run.get("head_branch"),
                "head_sha": run.get("head_sha"),
                "html_url": run.get("html_url"),
            },
            "failed_jobs": failed_jobs,
            "filtered_log": _scrub_ci_secrets(filtered.text),
            "bytes_raw": filtered.bytes_raw,
            "bytes_filtered": filtered.bytes_filtered,
            "anchor_count": filtered.anchor_count,
            "note": filtered.note,
        }
    )


# --------------------------------------------------------------------------
# analyze_playwright_trace
# --------------------------------------------------------------------------


def _llm_callable(ctx):
    """Adapt ctx.llm.complete_structured into the injectable diagnose() shape."""
    llm = getattr(ctx, "llm", None) if ctx is not None else None
    if llm is None or not hasattr(llm, "complete_structured"):
        return None

    def call(*, instructions, input, schema=None):
        return llm.complete_structured(instructions=instructions, input=input, schema=schema)

    return call


@_json_safe
def analyze_playwright_trace(params, ctx=None, **kwargs) -> str:
    p = _params(params)
    trace_path = str(p.get("trace_path") or "").strip()
    if not trace_path:
        return _err("'trace_path' is required")
    try:
        summary = trace.parse_trace(trace_path)
    except ValueError as exc:
        return _err(str(exc))

    diag = diagnose.diagnose(
        trace=summary,
        log_excerpt=p.get("log_excerpt"),
        complete_structured=_llm_callable(ctx),
    )
    return json.dumps({"diagnosis": diag, "trace": summary.brief()})


# --------------------------------------------------------------------------
# heal_flaky_test
# --------------------------------------------------------------------------


def _pull_ci_log(build_id: str, repo: str, transport) -> tuple[str | None, list[str]]:
    """Best-effort filtered CI log for diagnosis context; never fatal."""
    warnings: list[str] = []
    adapter, has_credentials = _make_ci_adapter(transport)
    if not has_credentials:
        # Diagnosis context is optional: degrade to a warning here. fetch_ci_logs,
        # where the log IS the request, returns an error envelope for the same case.
        return None, [f"build_id {build_id} given but GITHUB_TOKEN unset; skipped CI logs"]
    try:
        blob = adapter.download_logs_zip(repo, build_id)
        text = _logs_zip_to_text(blob)
        return _scrub_ci_secrets(logfilter.filter_log(text).text), warnings
    except (ci_base.CIError, *_ZIP_READ_ERRORS) as exc:
        warnings.append(f"could not fetch CI logs for run {build_id}: {exc}")
        return None, warnings


def _signature_for(trace_summary, diagnosis: dict | None) -> str:
    parts = (
        signature_mod.parts_from_trace(trace_summary)
        if trace_summary is not None
        else signature_mod.parts_from_diagnosis(diagnosis or {})
    )
    return signature_mod.signature_hash(parts)


def _pr_messages(report: healer.HealReport, sig: str, pre) -> tuple[str, str, str]:
    """Assemble ``(commit_message, pr_title, pr_body)`` for a heal PR.

    Separated from :func:`_pr_flow`'s dispatch orchestration so the outbound
    text — the only reviewer-facing product of the flow — lives in one place.
    """
    burnin = report.burnin or {}
    commit_message = (
        f"fix(flaky): heal {report.test_id} via {report.strategy}\n\n"
        f"Diagnosis: {report.diagnosis.get('cause')} — {report.diagnosis.get('evidence')}\n"
        f"Burn-in: {burnin.get('passed')}/{burnin.get('runs')} green "
        f"(isolation: {report.isolation})\n\n"
        "Generated by hermes-flaky-healer"
    )
    pr_title = f"fix(flaky): {report.test_id} ({report.strategy})"
    pr_body = (
        f"## Automated flaky-test heal\n\n"
        f"- **Test**: `{report.test_id}`\n"
        f"- **Cause**: {report.diagnosis.get('cause')} "
        f"(confidence {report.diagnosis.get('confidence')})\n"
        f"- **Strategy**: {report.strategy}\n"
        f"- **Reproduced pre-patch**: {report.reproduced}\n"
        f"- **Burn-in**: {burnin.get('passed')}/{burnin.get('runs')} green, "
        f"isolation `{report.isolation}`\n"
        f"- **Signature**: `{sig[:8]}`\n"
        f"- **Validated base**: `{pre.head_sha}` — burn-in ran against this commit; "
        f"the fix branch is cut from it\n\n"
        f"```diff\n{report.diff}```\n"
    )
    return commit_message, pr_title, pr_body


def _pr_flow(ctx, report: healer.HealReport, repo_dir: str) -> dict:
    sig = report.recipe_signature or _signature_for(None, report.diagnosis)
    branch = gitflow.branch_name(report.test_id, sig)
    base = config.base_branch()

    # Read-only preflight (via the same dispatch mechanism): refuses to start
    # the write sequence on a dirty tree and records the original ref (restored
    # on failure) plus the validated HEAD the fix branch is cut from.
    pre = gitflow.run_preflight(ctx, repo_dir)

    patch_dir = config.data_dir(ctx) / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / f"{branch.rsplit('/', 1)[-1]}.patch"
    patch_path.write_text(report.diff, encoding="utf-8")

    commit_message, pr_title, pr_body = _pr_messages(report, sig, pre)
    dispatches = gitflow.build_dispatches(
        repo_dir=repo_dir,
        branch=branch,
        base=base,
        patch_path=patch_path,
        commit_message=commit_message,
        pr_title=pr_title,
        pr_body=pr_body,
    )
    # Non-destructive failure cleanup: back to the user's original ref (the
    # preflight guarantees the tree was clean, so a forced checkout can only
    # discard our own half-applied patch), then drop the stale fix branch.
    cleanup = gitflow.cleanup_dispatches(repo_dir, pre.original_ref, branch=branch)
    results = gitflow.execute_via_ctx(ctx, dispatches, on_error=cleanup)
    return {
        "branch": branch,
        "base": base,
        "validated_head": pre.head_sha,
        "original_ref": pre.original_ref,
        "signature": sig,
        "patch_path": str(patch_path),
        "dispatches": dispatches,
        "results": results,
    }


@_json_safe
def heal_flaky_test(params, ctx=None, sandbox=None, store=None, transport=None, **kwargs) -> str:
    p = _params(params)
    repo_dir = str(p.get("repo_dir") or "").strip()
    test_id = str(p.get("test_id") or "").strip()
    if not repo_dir or not test_id:
        return _err("both 'repo_dir' and 'test_id' are required")
    mode = str(p.get("mode") or "suggest").strip().lower()
    if mode not in ("suggest", "pr"):
        return _err(f"unknown mode {mode!r}; expected 'suggest' or 'pr'")
    if mode == "pr" and ctx is None:
        return _err("mode='pr' requires the host context (approval pipeline unavailable)")

    warnings: list[str] = []
    trace_summary = None
    trace_path = str(p.get("trace_path") or "").strip()
    if trace_path:
        try:
            trace_summary = trace.parse_trace(trace_path)
        except ValueError as exc:
            return _err(str(exc))

    log_excerpt = None
    build_id = str(p.get("build_id") or "").strip()
    repo = str(p.get("repo") or "").strip()
    if build_id and repo:
        log_excerpt, warnings = _pull_ci_log(build_id, repo, transport)
    elif build_id and not repo:
        warnings.append("build_id given without repo; skipped CI logs")

    store = store or _store_for(ctx)
    report = healer.heal(
        repo_dir=repo_dir,
        test_id=test_id,
        trace_summary=trace_summary,
        log_excerpt=log_excerpt,
        strategy_override=p.get("strategy"),
        sandbox=sandbox,
        complete_structured=_llm_callable(ctx),
        store=store,
    )

    try:
        store.record_run(
            source="github-actions" if build_id else "local",
            build_id=build_id or None,
            test_id=test_id,
            diagnosis=report.diagnosis,
            strategy=report.strategy,
            result="stable" if report.stable else ("error" if report.error else "unstable"),
            isolation=report.isolation,
            duration_s=report.duration_s,
        )
    except Exception as exc:  # noqa: BLE001 — persistence must not kill the heal result
        warnings.append(f"could not record run: {exc}")

    payload: dict = {"mode": mode, "report": report.to_dict()}
    if warnings:
        payload["warnings"] = warnings

    if mode == "pr":
        reason = _pr_skip_reason(report)
        if reason is not None:
            payload["pr"] = None
            payload["pr_skipped"] = "refusing to open a PR: " + reason
        else:
            try:
                payload["pr"] = _pr_flow(ctx, report, repo_dir)
            except gitflow.GitFlowError as exc:
                payload["pr"] = None
                payload["pr_skipped"] = str(exc)
    return json.dumps(payload)


def _pr_skip_reason(report: healer.HealReport) -> str | None:
    """Why a stable-looking heal must NOT auto-open a PR — or ``None`` to proceed.

    Precedence is preserved from the original inline cascade: an error wins over
    an unstable burn-in, which wins over an empty diff, which wins over weak
    sandbox isolation.
    """
    if report.error:
        return report.error
    if not report.stable:
        return "post-patch burn-in was not fully green"
    if not report.diff.strip():
        return "no diff was produced"
    # A subprocess-validated heal ran the (possibly patched) test on the host
    # with no container isolation, so it must not be auto-promoted to a PR.
    if report.isolation == "subprocess" and not config.allow_subprocess_pr():
        return (
            "validation ran in the weaker 'subprocess' sandbox (no container "
            "isolation), so any patched code executed directly on the host; refusing "
            "to auto-open a PR. Re-run on the Docker backend, or set "
            "FLAKY_HEALER_ALLOW_SUBPROCESS_PR=1 to override."
        )
    return None


@_json_safe
def list_healing_recipes(params, ctx=None, store=None, **kwargs) -> str:
    store = store or _store_for(ctx)
    recipes = [r.to_dict() for r in store.all_recipes()]
    return json.dumps(
        {
            "count": len(recipes),
            "recipes": recipes,
            "db_path": str(store.db_path),
            "learned_patterns_md": str(store.db_path.parent / "learned-patterns.md"),
        }
    )


# --------------------------------------------------------------------------
# /heal slash command (optional nice-to-have, plan Phase 6.2)
# --------------------------------------------------------------------------


_USAGE = (
    "usage: /heal <repo_dir> <test_id> [suggest|pr] "
    "[trace_path=...] [strategy=...] [build_id=... repo=owner/name]"
)


def heal_command(args=None, ctx=None, **kwargs) -> str:
    """'/heal <repo_dir> <test_id> [mode] [key=value…]' → human summary."""
    try:
        return _heal_command(args, ctx=ctx, **kwargs)
    except Exception as exc:  # noqa: BLE001 — slash commands must return text, never raise
        log.debug("heal_command failed", exc_info=True)
        return f"heal failed: {type(exc).__name__}: {exc}"


def _heal_command(args=None, ctx=None, **kwargs) -> str:
    text = args if isinstance(args, str) else " ".join(str(a) for a in (args or []))
    positional: list[str] = []
    params: dict = {}
    for token in text.split():
        if "=" in token:
            key, _, value = token.partition("=")
            params[key] = value
        else:
            positional.append(token)
    if len(positional) < 2:
        return _USAGE
    params["repo_dir"] = positional[0]
    params["test_id"] = positional[1]
    if len(positional) > 2:
        params["mode"] = positional[2]
    data = json.loads(heal_flaky_test(params, ctx=ctx, **kwargs))
    if "error" in data:
        return f"heal failed: {data['error']}"
    report = data["report"]
    burnin = report.get("burnin") or {}
    lines = [
        f"test: {report['test_id']}",
        f"cause: {(report.get('diagnosis') or {}).get('cause')} "
        f"(stage: {report.get('diagnosis_stage')}, recipe: {report.get('recipe_used')})",
        f"strategy: {report.get('strategy')}",
        f"reproduced: {report.get('reproduced')} | burn-in: "
        f"{burnin.get('passed')}/{burnin.get('runs')} green | stable: {report.get('stable')}",
        f"isolation: {report.get('isolation')}",
    ]
    if report.get("error"):
        lines.append(f"error: {report['error']}")
    if data.get("pr"):
        lines.append(f"pr branch: {data['pr']['branch']}")
    if data.get("pr_skipped"):
        lines.append(f"pr skipped: {data['pr_skipped']}")
    if report.get("diff"):
        lines.append("\n" + report["diff"])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Observer hooks (fire-and-forget; must never raise — plan §3.4)
# --------------------------------------------------------------------------


def _audit_event(event: str, args: tuple, kwargs: dict, ctx=None) -> None:
    payload = {"args": redact_mod.redact(list(args)), "kwargs": redact_mod.redact(kwargs)}
    try:
        json.dumps(payload)
    except TypeError:
        # Repr the REDACTED payload, never the raw inputs: raw args next to a
        # non-serializable object would bypass the sensitive-key check.
        payload = redact_mod.repr_fallback(payload)
    _store_for(ctx).audit(event, payload)
    log.debug("audit %s: %s", event, json.dumps(payload)[:500])


def on_pre_approval_request(*args, ctx=None, **kwargs) -> None:
    try:
        _audit_event("pre_approval_request", args, kwargs, ctx=ctx)
    except Exception:  # noqa: BLE001 — observers must be exception-safe
        log.debug("pre_approval_request audit failed", exc_info=True)


def on_post_approval_response(*args, ctx=None, **kwargs) -> None:
    try:
        _audit_event("post_approval_response", args, kwargs, ctx=ctx)
    except Exception:  # noqa: BLE001
        log.debug("post_approval_response audit failed", exc_info=True)
