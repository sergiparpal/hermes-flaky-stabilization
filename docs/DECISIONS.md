# Decisions

## Phase 0 checkpoint (2026-07-09, answered by sergiparpal)

| # | Question | Answer |
|---|---|---|
| Q1 | Plugin name stays `hermes-flaky-stabilization`? | **Yes** — manifest name, package `hermes_flaky_stabilization`, data dir `<hermes_home>/flaky-stabilization/`, CLI `flaky-stab`. |
| Q2 | Build net-new `jira_create_incident` write tool? | **Yes, build it, ship disabled** — `jira.enable_write: false` default, `check_fn` hides it without `JIRA_API_TOKEN`, approval-escalated via `pre_tool_call`, PII-gated (D6/D7). |
| Q3 | License GPL-3.0-only? | **Yes** — forced by absorbed GPLv3 code; the repo's initial MIT LICENSE is replaced. |
| Q4 | Keep `test-history` CLI alias? | **Yes** — `hermes test-history ingest|status|prune|rebuild-fts|config` unchanged (D2). |

Preflight: `scripts/preflight.sh` passed on 2026-07-09 (7 sibling repos, hermes-agent 0.18.2 at
`/home/sergi/hermes-agent`, Python 3.12.3, pytest 7.4.4, sqlite FTS5 available).

## Resolved design decisions (D1–D10)

Imported verbatim from PLAN.md §5 when the plan was deleted after full implementation
(2026-07-09). Cross-references like §2, §3.x, §10, §11 and Appendices A/B point into the
deleted plan — recover it with `git show 950c72b:PLAN.md`.

**D1 — Category/kind and the tracker stage.** The unified plugin is **`kind: standalone`** (the
real manifest field; the proposal's `category: general` does not exist). `jira-incidents` is folded
in as three plain tools (toolset `jira_incidents`) registered with `ctx.register_tool`, dropping
the `MemoryProvider` class entirely (a standalone plugin cannot register one — §2 row 16). PII
protection is preserved and strengthened: (a) the `redaction.py` module is ported verbatim and
every incident-facing tool response keeps its existing redact-then-emit path; (b) the lost
automatic `prefetch()` is replaced by a **`pre_llm_call` hook** (config-gated,
`incidents.context_injection`, default on) that runs a local-FTS-only, timeout-bounded (1.5 s),
redacted lookup of the user message and returns `{"context": ...}` — never a network call on the
turn path; (c) background Jira sync keeps the ported `SyncScheduler` (coalesced, debounced),
triggered non-blockingly from `on_session_start` and before incident tool reads, plus the optional
cron (D10). Net effect: the exclusive `memory.provider` slot is freed for other providers.

**D2 — `test-history` stays a keystone, absorbed with a frozen contract.** Absorb the code (one
repo to maintain) but freeze its public contract: identical tool names, schemas, toolset
(`test_history`), return shapes, error envelopes; identical DB path
**`<hermes_home>/test-history/history.db`** with `schema_version` pinned at 1 (external plugins —
`flaky-detective` itself proves it, plus release-readiness/regression-selector/api-fuzzer/
coverage-history per the proposal — consume both the tools *and* the raw DB file); and the
`test-history` CLI command name kept as an alias (CI scripts run `hermes test-history ingest`).
Alternative rejected: keeping `hermes-test-history` as an external dependency would leave a
two-plugin install, cross-repo version skew, and no way to fix the enrichment bug internally.

**D3 — Internal architecture: per-stage packages + a thin orchestrator.** One importable package
`hermes_flaky_stabilization` (with the proven root-shim `__init__.py` pattern from
bug-report-improver so the hyphenated plugin dir loads under Hermes and plain pytest alike).
Stage subpackages preserve each plugin's ports-and-adapters shape — **only the top-level
`__init__.py` and `registration.py` may import Hermes**; stages receive `llm`, `dispatch_tool`,
`hermes_home` as injected parameters (ci-triage's discipline, generalized). The orchestrator is
plain Python control flow (the fork and both feedback loops), calling stage functions directly —
no internal `dispatch_tool` except for host git/PR tools, which must keep going through the
approval pipeline.

**D4 — Two databases, not one and not six.** (1) `history.db` untouched at its current path — it
is a public, file-level data contract (D2). (2) A new consolidated
**`<hermes_home>/flaky-stabilization/state.db`** for all private state: detective verdicts + scan
runs, triage patterns (+FTS), healer runs/recipes/audit, incidents (+FTS)/links/meta, and a new
`pipeline_runs` ledger — full DDL in Appendix A, with healer-style versioned migrations
(`SCHEMA_VERSION`, ladder of idempotent steps). Migration of existing data is a CLI command that
**copies** from the four legacy DBs (originals untouched → trivial rollback), §10.

**D5 — Consolidated configuration.** `plugin.yaml` (Appendix B) declares `kind: standalone` and
**no `requires_env`** — both credentials are optional per-stage (`GITHUB_TOKEN` gates
`fetch_ci_logs` via `check_fn`; `JIRA_API_TOKEN` gates Jira sync/tools via `check_fn`), so a
missing token must not disable the whole plugin (§3.2). One JSON config file
`<hermes_home>/flaky-stabilization/config.json` with per-stage sections (defaults in Appendix B):
`history`, `detective` (window/min_fails/include_errors/schedule/deliver), `triage`, `healer`,
`pii`, `jira` (base_url/email/auth_mode/jql/field mapping/retention/**enable_write: false**),
`incidents` (context_injection, prefetch limits), `pipeline` (approvals + gate policy). Legacy
env vars (`FLAKY_HEALER_*`, `HERMES_CI_TRIAGE_*`, `JIRA_*`, `HERMES_JIRA_STRICT_REDACTION`) keep
working as overrides for compatibility; the config file is the documented home.

**D6 — Approvals and safety policy.** Four layers, all mechanically enforced:
1. **Sandbox-only modification**: the healer's copy-then-patch model is kept verbatim (Docker
   preferred; subprocess fallback keeps its PR-refusal guard).
2. **PR-only git flow through host approvals**: git/PR steps remain `ctx.dispatch_tool` calls to
   `terminal`/`create_pull_request` (configurable), so the host approval/redaction/budget pipeline
   applies; direct pushes to default branches stay structurally impossible.
3. **Plugin-side approval escalation** (net-new, the *correct* Hermes mechanism per §3.5): a
   `pre_tool_call` hook that returns `{"action":"approve", "message":..., "rule_key":...}` for
   `heal_flaky_test` with `mode="pr"` and for `jira_create_incident` (fail-closed). The healer's
   existing `pre_approval_request`/`post_approval_response` **audit observers** are kept — and the
   plan explicitly corrects the proposal: those hooks cannot gate anything.
4. **PII gate before any external output**: `jira_create_incident` (D7) refuses unless
   (a) every referenced evidence path passed `validate_no_pii` with `clean && complete` within the
   same call, and (b) all outbound text fields have passed `redaction.redact_text`. The
   orchestrator enforces the same gate on its bug branch. The gate is in the handler (deterministic),
   not in a hook (advisory).

**D7 — Tracker write-back is net-new and off by default.** No legacy code pushes to Jira (§2
row 9). The pipeline still needs an endpoint, so the plan adds a minimal, clearly-labeled-new tool
**`jira_create_incident`** (POST `/rest/api/{v}/issue`, reusing the hardened `jira_client`
transport), config-gated by `jira.enable_write: false`, `check_fn`-hidden without
`JIRA_API_TOKEN`, approval-escalated and PII-gated per D6. When disabled, the pipeline ends by
returning the structured, redacted ticket body for the user to file manually.

**D8 — The seven repos are deprecated and their code absorbed.** Code and tests are copied into
this repo (monorepo per stage, D3). Rationale: the tight dataflow coupling and the enrichment-bug
class of cross-repo contract drift are exactly what unification removes; coexistence is impossible
anyway because tool names would collide in the registry (§3.3). Each legacy repo gets a final
release + archived status + README pointer (§11). License: the unified repo is **GPL-3.0-only**
(several sources are GPL; absorbing their code forces the union to GPL — note that
flaky-detective's README wished otherwise, but its LICENSE file is GPLv3; record this in the repo
README).

**D9 — The orchestrator implements the fork and both feedback loops (all net-new).** New tool
**`stabilize_test_failure`** (+ `/stabilize` slash command): ingest-or-locate failure evidence →
`is_flaky` + history lookups → `triage_pipeline_failure` (with **fixed** internal enrichment and
net-new incident-context enrichment: a redacted local-FTS lookup of the incidents index seeds the
triage prompt's prior-hint block — this implements the proposal's "jira → ci-triage feedback" for
real) → **fork** on category: `flaky`/`timeout` → healer branch (suggest or pr per policy); any
other category → bug branch: `improve_bug_report` → **`find_duplicate_incidents`** (net-new tool:
local FTS + trigram-ish token overlap against the incidents index — the honest version of the
proposal's fabricated `search_possible_duplicates`; no tracker round-trip needed) → PII gate →
`jira_create_incident` if enabled, else return the ticket body. **Loop closure**: after a stable
heal, the orchestrator writes the burn-in outcome back into `history.db` as a synthetic
`test_runs`/`test_cases` row set (source `flaky-healer-burnin`), so the next detective scan sees
the recovery — implementing the proposal's "fix feeds back into test-history" for real. Every
pipeline run is recorded in `pipeline_runs` (Appendix A).

**D10 — Cron follows the proven imperative pattern.** `hermes flaky-stab install-cron
[--schedule "0 9 * * *"] [--deliver ...] [--no-create]`: persists schedule to config, installs a
shim `flaky-stab-scan.sh` into `$HERMES_HOME/scripts/`, and shells
`hermes cron create <schedule> --no-agent --script flaky-stab-scan.sh --deliver <d> --name
flaky-stabilization` (printing the copy-paste command when the CLI/gateway is unavailable). The
shim runs `hermes flaky-stab scan --format cron` (detective sweep; changes-only output; empty
stdout = silent tick). Optionally the same subcommand can install a second job for Jira sync
(`--with-jira-sync`, runs `hermes flaky-stab jira sync --quiet`).
