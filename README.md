# hermes-flaky-stabilization

**One Hermes Agent plugin for the whole flaky-test stabilization pipeline**:
JUnit failure history, flaky detection, CI-log triage, sandboxed healing with a
PR-only git flow, bug-report structuring, PII gating, and a local Jira incident
index — absorbed from seven predecessor plugins into a single `kind: standalone`
plugin with one private state store.

> Supersedes: `hermes-test-history`, `hermes-flaky-detective`,
> `hermes-ci-triage`, `hermes-flaky-healer`, `hermes-bug-report-improver`,
> `hermes-masking-validator`, `hermes-jira-incidents`. See `MIGRATION.md`.

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

## License

GPL-3.0-only — several absorbed plugins are GPLv3, which the union inherits
(plan D8). See `LICENSE`.

*Full feature map, configuration reference, and security model land with the
final release docs (plan Phase 7).*
