# Definition of Done — verification report

Plan §12 checklist, verified by the implementing agent on **2026-07-09**
against hermes-agent **0.18.2** (`/home/sergi/hermes-agent`), Python 3.12.3.
Final state: **938 passed, 1 skipped (OCR unavailable), 1 deselected
(docker/live markers)**; coverage **89 %** (gate: ≥ 85 %); `ruff check .`
clean.

- [x] **`plugin.yaml` + root shim + `register(ctx)` load under the real
  `PluginManager` with zero errors** — `tests/integration/test_discovery.py::
  test_plugin_loads_and_registers_full_surface` (subprocess-pristine load into
  a throwaway `HERMES_HOME`, opted in via `plugins.enabled`).
- [x] **All 13 legacy tools registered with byte-identical schemas and
  JSON-parity behavior; the 3 new tools registered and snapshot-frozen** —
  `tests/test_schemas.py` (16 snapshots; the 13 legacy ones dumped from the
  *legacy plugins' source*), `tests/test_parity_phase2.py` (17 cases),
  `tests/test_parity_phase3.py` (4 cases) against the live sibling checkouts.
- [x] **`history.db` path, schema (v1) and CLI alias unchanged** — history
  stage ported verbatim (own storage module, PRAGMAs, hardened parser);
  `tests/test_cli.py::test_test_history_alias_exposes_history_cli`; verified
  in the real registry (`test-history` in `pm._cli_commands`).
- [x] **All ported suites green offline; coverage ≥ 85 %; ruff clean** —
  `bash scripts/run_tests.sh` (no docker/network/credentials): 938 passed;
  coverage 89 %; `ruff check .` → "All checks passed!".
- [x] **`HERMES_REPO=… run_tests.sh` green including real-loader integration**
  — same run includes `tests/integration/` (2 tests, subprocess drivers).
- [x] **No-PII-leak suite green against the new tool handlers; PII gate blocks
  a seeded-dirty run; `jira_create_incident` redacts outbound fields and is
  hidden/off by default** — `tests/incidents/test_no_pii_leak.py` (ported,
  incl. the new `llm_context` path), `tests/orchestrator/test_pipeline.py::
  test_dirty_evidence_stops_pipeline_with_no_tracker_call`,
  `tests/orchestrator/test_hooks_and_write.py` (POST body carries
  `[redacted-email]`; error paths never echo the request; check_fn matrix).
- [x] **Approval escalation directives for `heal_flaky_test(mode=pr)` and
  `jira_create_incident`; healer PR flow still refuses unstable/empty/
  subprocess-isolated heals; git/PR steps exclusively via `ctx.dispatch_tool`**
  — `tests/orchestrator/test_hooks_and_write.py` (directives + rule keys);
  the ported healer suite (239 tests) covers the refusal guards and the
  dispatch-only git flow; `tests/test_security_scan.py` pins the subprocess
  allowlist.
- [x] **`pre_llm_call` incident injection: bounded, redacted, config-gated,
  offline** — `tests/test_incidents_wiring.py` (`[redacted-email]` in the
  payload, 0.2 s slow-store bound, `incidents.context_injection` off → None,
  never raises on a fresh profile).
- [x] **`flaky-stab migrate` idempotent, sources untouched (hash-verified),
  provenance recorded** — `tests/test_migrate.py` (all four fixture DBs incl.
  a v1-shaped healer.db with relaxed-key backfill; run-twice no-op;
  SHA-256-identical sources; `--dry-run` writes nothing; `migrated_from_*`
  provenance rows).
- [x] **`install-cron` installs the no-agent job (or prints the fallback)** —
  ported `tests/detective/test_install_cron.py` (renamed shim/job per
  Appendix C) + `tests/test_migrate.py::test_install_cron_with_jira_sync`.
- [x] **`scripts/smoke.sh` passes on a disposable profile** — real
  PluginManager load, migrate, 4-run JUnit ingest, `scan` flags the seeded
  test flaky, `stabilize_test_failure` envelope printed (heuristic triage,
  valid category), status shows migrated counts, cron shim staged.
- [x] **README, MIGRATION.md, after-install.md, DECISIONS.md, DoD-REPORT.md
  committed** — this file completes the set.
- [x] **The kind-coercion marker strings absent** — `tests/test_register.py::
  test_entry_files_free_of_memory_provider_markers` (entry files) and
  `tests/test_incidents_wiring.py::test_memory_marker_strings_absent_from_
  entire_package` (package-wide).
- [x] **Zero modifications to Hermes core or the seven legacy repos** —
  `git status --porcelain` clean in all eight checkouts at completion.

## Notable findings during implementation

* **Fixed live bug (plan §2 row 6):** ci-triage's enrichment dispatched
  `test_failure_lookup`/`module_failure_history` with arg key `"query"`;
  the unified plugin calls the in-package history query directly and a
  regression test pins that real lookup data (never an error envelope)
  reaches the classifier.
* **New bug found by Phase 8:** the legacy healer passed `str(path)` to
  `register_skill`, but core calls `path.exists()` on the argument — the
  skill silently never registered under core 0.18.2. Fixed (pass `Path`);
  caught only by the real-loader integration test.
* **Hardening added:** `state.db`'s migration step tolerates a pre-existing
  legacy (healer-v1) `recipes` table without `relaxed_key`.

## Sanctioned deviations from verbatim porting

Each is listed in its phase's commit message; the classes are: import-path
adaptations, storage adapters onto `state.db` (Appendix A table renames),
the enrichment fix, the healer data-dir default, CLI renames mandated by
Appendix C (`flaky-detective` → `flaky-stab`, shim/job names, remediation
strings), removal of the masking-validator dual-import shim, the
memory-slot lifecycle drop for incidents (D1), and linter-driven type-
annotation modernization (behavior-preserving).
