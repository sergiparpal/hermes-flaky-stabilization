# hermes-flaky-stabilization

**One Hermes Agent plugin for the whole flaky-test stabilization pipeline**:
JUnit failure history, flaky detection, CI-log triage, sandboxed healing with a
PR-only git flow, bug-report structuring, PII gating, and a local Jira incident
index — absorbed from seven predecessor plugins into a single `kind: standalone`
plugin with one private state store.

> Supersedes: `hermes-test-history`, `hermes-flaky-detective`,
> `hermes-ci-triage`, `hermes-flaky-healer`, `hermes-bug-report-improver`,
> `hermes-masking-validator`, `hermes-jira-incidents`. See `MIGRATION.md`.

## Install & enable

```bash
hermes plugins install sergiparpal/hermes-flaky-stabilization
hermes plugins enable hermes-flaky-stabilization
# disable the legacy plugins first — tool names would otherwise collide:
hermes plugins disable hermes-test-history hermes-flaky-detective hermes-ci-triage \
  hermes-flaky-healer hermes-bug-report-improver hermes-masking-validator
# if hermes-jira-incidents was your memory provider, unset memory.provider too.
hermes flaky-stab migrate       # copy legacy data into state.db (sources untouched)
```

## Feature map

| Stage | Tools (toolset) | CLI |
|---|---|---|
| Failure history (keystone, contract frozen) | `test_failure_lookup`, `module_failure_history` (`test_history`) | `flaky-stab ingest\|prune\|rebuild-fts\|config`; alias `hermes test-history …` |
| Flaky detection | `is_flaky` (`flaky_detective`) | `flaky-stab scan\|list\|install-cron` |
| CI triage | `triage_pipeline_failure` (`ci_triage`) | — |
| Sandboxed healing | `fetch_ci_logs`*, `analyze_playwright_trace`, `heal_flaky_test`, `list_healing_recipes` (`flaky_healer`) | `/heal` slash command; `flaky-healer` skill |
| Bug-report structuring | `improve_bug_report` (`qa`) | `/improve-bug` |
| PII validation | `validate_no_pii` (`qa_masking`) | — |
| Incident index | `jira_search_incident`, `jira_get_root_cause`, `jira_link_session`, `jira_create_incident`*† (`jira_incidents`) | `flaky-stab jira status\|sync\|config` |
| Orchestration (net-new) | `stabilize_test_failure`, `find_duplicate_incidents` (`flaky_stabilization`) | `/stabilize`; `flaky-stab status\|migrate` |

\* hidden without its credential (`GITHUB_TOKEN` / `JIRA_API_TOKEN`) —
credentials are deliberately **not** manifest `requires_env`, so a missing
token never disables the whole plugin.
† additionally requires `jira.enable_write: true` (default **false**) and is
approval-escalated on every call.

### The pipeline (`stabilize_test_failure`)

history → detection → triage (with test-history enrichment **and** a
related-incidents hint from the local index), then a fork on the triage
category:

* `flaky` / `timeout` → the healer (suggest or PR mode). A stable burn-in is
  written back into `history.db` as a synthetic run, so the next detection
  sweep sees the recovery.
* anything else → `improve_bug_report` → `find_duplicate_incidents` →
  **PII gate** → `jira_create_incident` when enabled, else the redacted ticket
  body is returned to file manually (`outcome: ticket_ready`).

Every run lands in the `pipeline_runs` ledger inside `state.db`.

## Storage

* `<hermes_home>/test-history/history.db` — **public, file-level data
  contract** (schema v1). Never moved; other plugins may read it.
* `<hermes_home>/flaky-stabilization/state.db` — all private state: verdicts,
  scan runs, triage patterns (+FTS), healer runs/recipes/audit, incidents
  (+FTS), links, meta, pipeline runs. Owner-only (`0700` dir / `0600` file),
  WAL, versioned migration ladder.

## Configuration

One JSON file: `<hermes_home>/flaky-stabilization/config.json`. All keys
optional; malformed files degrade to defaults. Sections (defaults in
parentheses): `history` (lookback 30d, stack-trace cap 500),
`detective` (window 14d, min_fails 3, include_errors, schedule `0 9 * * *`),
`triage` (enable_enrichment), `healer` (burnin `5:10`, sandbox auto, base
branch main), `pii` (max_files 2000), `jira` (base_url, email, jql,
retention, **enable_write: false**, project_key INC, issue_type Bug),
`incidents` (context_injection on, prefetch limit 3 / timeout 1.5s),
`pipeline` (default_heal_mode suggest, heal_categories [flaky, timeout],
require_pii_gate true — never set false in production).

Precedence: env var > `config.json` section > (for `history` only) the
legacy `test-history/config.json` > built-in default. Legacy env vars keep
working as overrides: `FLAKY_HEALER_*`, `HERMES_CI_TRIAGE_*`,
`JIRA_BASE_URL`/`JIRA_EMAIL`, `HERMES_JIRA_STRICT_REDACTION`. Secrets live
**only** in env: `GITHUB_TOKEN`, `JIRA_API_TOKEN`.

## Security model

1. **Sandbox-only modification** — the healer patches a temporary copy of the
   project inside a hardened Docker sandbox (digest-pinned image, no network,
   caps dropped) or a weaker subprocess fallback that refuses PR mode unless
   explicitly allowed. The original tree is never touched.
2. **PR-only git flow through host approvals** — git/PR steps are
   `ctx.dispatch_tool` calls (`terminal`, `create_pull_request`), so the host
   approval/redaction/budget pipeline applies; pushes to default branches are
   structurally impossible. The flow refuses to start on a dirty working
   tree, cuts the fix branch from the burn-in-validated HEAD (`base` is the
   PR target only; the validated sha is recorded in the PR body), treats any
   host result that does not positively signal success as a failure, and on
   any failure restores the original ref and deletes the fix branch.
3. **Plugin-side approval escalation** — a `pre_tool_call` hook returns
   `{"action": "approve"}` for `heal_flaky_test(mode=pr)`, every
   `jira_create_incident` call, and `stabilize_test_failure` whenever the
   run could write (PR mode requested, or the Jira write path is live) —
   fail-closed at the host, and fail-closed to escalation when the config
   cannot be read.
4. **PII gate before any external output** — `jira_create_incident` (and the
   pipeline's bug branch) refuse unless every referenced evidence file passes
   `validate_no_pii` (`clean && complete`) in the same call, and every
   outbound field is redacted (`[redacted-*]` tokens). Incident data is
   redacted on *every* model-facing path; the local index keeps full fidelity
   at rest, owner-only.
5. **Untrusted-content framing** — CI logs, test artifacts, and incident text
   are always fenced as untrusted data in prompts and tool output.

## Triage taxonomy

`triage_pipeline_failure` classifies every CI failure into exactly one of six
categories (single source of truth: `hermes_flaky_stabilization/triage/taxonomy.py`):

| Category | Meaning | Typical next action |
|---|---|---|
| `broken_test` | The test itself is wrong (bad assertion, stale fixture, outdated selector) | Fix the test code |
| `environment` | The CI environment broke (missing dependency, version mismatch, config error) | Fix the CI image/config |
| `data` | Test data or fixtures are wrong/missing/stale | Refresh the data/fixture |
| `timeout` | The run exceeded a time limit (slow test, hang, saturated runner) | Investigate the slowness; maybe raise the limit |
| `flaky` | Intermittent, non-deterministic failure (race, ordering, external service blip) | Send it to the healer; quarantine or retry |
| `infra` | CI infrastructure failure (runner died, network partition, disk full, registry down) | Retry; escalate to infra |

## Nightly automation

```bash
hermes flaky-stab install-cron --schedule "0 9 * * *" [--with-jira-sync]
```

Installs a no-agent shim (`flaky-stab-scan.sh`) that runs
`hermes flaky-stab scan --format cron` — silent on quiet nights, alerting on
new flaky tests — and optionally a second job that runs
`hermes flaky-stab jira sync --quiet` (silent when nothing changed; a broken
sync exits non-zero so the job alerts). `hermes flaky-stab jira sync --full`
re-ingests from scratch and, when the run completes untruncated, removes
locally indexed incidents that no longer exist in Jira. The gateway daemon
must be running for jobs to fire.

## Development

```bash
bash scripts/run_tests.sh            # the only sanctioned test entry (offline)
HERMES_REPO=~/hermes-agent bash scripts/run_tests.sh   # + real-loader integration
ruff check .
```

Requires Python ≥ 3.11, < 3.14. Runtime is standard library only; OCR extras
for the PII scanner are optional (`requirements-ocr.txt`).

## License

GPL-3.0-only — several absorbed plugins are GPLv3, which the union inherits
(plan D8; note `hermes-flaky-detective`'s README wished otherwise, but its
LICENSE file is GPLv3). See `LICENSE`.
