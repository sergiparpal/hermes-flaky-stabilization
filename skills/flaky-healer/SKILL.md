---
name: flaky-healer
description: Diagnose and heal flaky Playwright tests from CI failures using sandboxed burn-in validation and learned healing recipes.
---

# Flaky Healer

Use this workflow when a CI run fails and the failure looks flaky (timeouts,
selector misses, race conditions) rather than a genuine regression.

## Workflow

1. **Pull the failure context.** If the failure came from GitHub Actions, call
   `fetch_ci_logs(build_id, repo)` — it returns run metadata, the failed jobs and
   a pre-filtered (≤ ~15 KB) log fragment. Never paste raw multi-MB logs.
2. **Diagnose.** With a Playwright `trace.zip` in hand (CI artifact or local
   repro), call `analyze_playwright_trace(trace_path)`. It returns a structured
   diagnosis: `cause` (timeout, fragile_selector, race_condition, environment,
   data, infra, unknown), evidence, the failing action/selector, a recommended
   fix strategy and a confidence score.
3. **Heal.** Call `heal_flaky_test(repo_dir, test_id, trace_path=..., mode="suggest")`.
   The tool reproduces the flake in an isolated sandbox, applies the fix
   strategy to a copy of the repo, and validates by burn-in (M reproduce runs,
   then N/N green required post-patch). `suggest` mode returns the diff and the
   full report for review.
4. **Open a PR.** Re-run with `mode="pr"` once the diff looks right. Every git
   write (branch, commit, push, PR) is dispatched through the host's approval
   pipeline — nothing is pushed without your approval policy seeing it.
5. **Inspect what it learned.** `list_healing_recipes()` shows persisted
   recipes (failure signature → fix procedure) with hit/success/failure stats.
   Repeat failures matching a recipe skip re-diagnosis but always re-validate
   with a full burn-in.

## Fix strategies

- `bump_timeout` — raise the failing action/assertion timeout (≤3×, cap 60 s).
- `testid_selector` — replace a fragile selector with `getByTestId(...)`, only
  when the trace DOM snapshot proves the target carries a `data-testid`.
- `await_state` — for a target implicated by unsettled network traffic, raise
  an explicitly short assertion timeout to 5 seconds or wait for that target
  locator to become visible. It deliberately avoids a global `networkidle`
  wait, which is often unrelated to the failing element.

## Notes

- `fetch_ci_logs` only appears when `GITHUB_TOKEN` is set.
- Results carry an `isolation` tag: `docker` (hardened, network-less container)
  or `subprocess` (weaker fallback; treat with more skepticism). A
  `subprocess`-validated heal will **not** open a PR by default — it ran on the
  host with no container isolation; re-run on the Docker backend, or set
  `FLAKY_HEALER_ALLOW_SUBPROCESS_PR=1` to override.
- A learned recipe is never applied without a fresh full burn-in.
- Trace summaries are deliberately bounded and redacted: secrets and PII are
  masked, and network URLs are reduced to their origin. Use the local trace
  artifact for raw forensic inspection rather than expecting those values in
  tool output.
