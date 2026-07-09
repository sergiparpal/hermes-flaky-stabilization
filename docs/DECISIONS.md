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
