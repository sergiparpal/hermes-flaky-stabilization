# Migrating from the seven legacy plugins

`hermes-flaky-stabilization` absorbs `hermes-test-history`,
`hermes-flaky-detective`, `hermes-ci-triage`, `hermes-flaky-healer`,
`hermes-bug-report-improver`, `hermes-masking-validator`, and
`hermes-jira-incidents`. All 13 legacy tools keep their names, schemas, and
JSON return shapes.

## Steps

1. **Install + enable the unified plugin; disable the legacy ones.** The tool
   names collide in the registry, so the six standalone legacy plugins must be
   disabled first:

   ```bash
   hermes plugins install sergiparpal/hermes-flaky-stabilization
   hermes plugins enable hermes-flaky-stabilization
   hermes plugins disable hermes-test-history hermes-flaky-detective \
     hermes-ci-triage hermes-flaky-healer hermes-bug-report-improver \
     hermes-masking-validator
   ```

   If `memory.provider` in `config.yaml` points at `hermes-jira-incidents`,
   unset it — the incident tools are plain tools now, and the exclusive memory
   slot is free for any other provider.

2. **Copy the legacy data:**

   ```bash
   hermes flaky-stab migrate            # add --dry-run to preview
   ```

   Copies detective verdicts + scan runs, ci-triage patterns, healer
   runs/recipes/audit (v1 recipes get the relaxed-key backfill), and the
   incident index (+ links + sync watermark) into
   `flaky-stabilization/state.db`. Sources are opened read-only and left
   byte-identical; re-running adds nothing (provenance rows in `meta` record
   what was migrated when). `history.db` needs no migration — unchanged path
   and schema. If your healer data dir was relocated via
   `FLAKY_HEALER_DATA_DIR`, point the importer at it:
   `hermes flaky-stab migrate --healer-db <dir>/healer.db`.

3. **Re-point crontabs.** Delete the old `flaky-detective` job and install the
   new one:

   ```bash
   hermes cron delete flaky-detective     # if it exists
   hermes flaky-stab install-cron [--with-jira-sync]
   ```

4. **Env vars are unchanged**: `GITHUB_TOKEN`, `JIRA_API_TOKEN`,
   `FLAKY_HEALER_*`, `HERMES_CI_TRIAGE_*`, `JIRA_BASE_URL`/`JIRA_EMAIL`,
   `HERMES_JIRA_STRICT_REDACTION` all still work.

## Configuration mapping

| Legacy | Unified (`flaky-stabilization/config.json`) |
|---|---|
| `test-history/config.json` | `history` section |
| `flaky-detective/config.json` | `detective` section |
| `hermes-jira-incidents.json` | `jira` + `incidents` sections |
| ci-triage / healer env-only config | `triage` / `healer` sections (env still wins) |

## Breaking changes (documented, accepted)

* **Skill qualifier**: `hermes-flaky-healer:flaky-healer` →
  `hermes-flaky-stabilization:flaky-healer`.
* **Healer data dir default** moved from `plugins-data/hermes-flaky-healer/`
  to `flaky-stabilization/` (the `FLAKY_HEALER_DATA_DIR` override preserves
  any old location); the healer DB is the consolidated `state.db`.
* **jira-incidents' automatic system-prompt block is gone** — replaced by a
  per-turn `pre_llm_call` context injection (config: `incidents.context_injection`).
* **CLI renames**: `hermes flaky-detective …` → `hermes flaky-stab …`;
  `hermes hermes-jira-incidents …` → `hermes flaky-stab jira …`. The
  `hermes test-history …` CLI is kept as an alias (CI scripts keep working).
* Remediation strings inside tool output now name `flaky-stab` commands.

## Net-new (not in any legacy plugin)

* `stabilize_test_failure` + `/stabilize` — the orchestrated pipeline.
* `find_duplicate_incidents` — local duplicate search over the incident index.
* `jira_create_incident` — tracker write-back, **off by default**
  (`jira.enable_write: false`), hidden without `JIRA_API_TOKEN`,
  approval-escalated, PII-gated.

## Rollback

Disable the unified plugin, re-enable the legacy seven (and re-set
`memory.provider` if desired). Migration copies rather than moves, so all
legacy state is exactly where it was. Data written to `state.db` *after*
migration does not flow back — rollback loses only post-migration
verdicts/patterns/recipes/incident deltas; `history.db` loses nothing since
both worlds share it.
