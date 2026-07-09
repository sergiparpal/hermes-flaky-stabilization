# PLAN.md — `hermes-flaky-stabilization`

**Development plan for a unified Hermes Agent plugin that merges the seven-plugin flaky-test
stabilization pipeline into a single `standalone` plugin.**

This document is self-contained. An autonomous coding agent (e.g. Claude Code) can implement the
plugin from this file alone, without the original proposal document. All technical claims below were
verified against the real source code of the seven plugins and the Hermes Agent core checkout
(`/home/sergi/hermes-agent`, version **0.18.2**, `requires-python >= 3.11, < 3.14`) on 2026-07-09.
Where the original proposal was wrong, this plan records the correction (§2) and builds on reality.

---

## Table of contents

1. [Mission and scope](#1-mission-and-scope)
2. [Ground truth: verified corrections to the proposal](#2-ground-truth-verified-corrections-to-the-proposal)
3. [Hermes plugin platform reference (verified against core 0.18.2)](#3-hermes-plugin-platform-reference)
4. [Source plugin inventory (verified)](#4-source-plugin-inventory)
5. [Resolved design decisions](#5-resolved-design-decisions)
6. [Target architecture](#6-target-architecture)
7. [Implementation phases](#7-implementation-phases)
8. [Testing strategy](#8-testing-strategy)
9. [Install, load and smoke-test inside Hermes](#9-install-load-and-smoke-test-inside-hermes)
10. [Migration and rollback](#10-migration-and-rollback)
11. [Strategy for the seven existing repos](#11-strategy-for-the-seven-existing-repos)
12. [Definition of Done](#12-definition-of-done)
13. [Appendix A — unified `state.db` DDL](#appendix-a--unified-statedb-ddl)
14. [Appendix B — consolidated config file spec](#appendix-b--consolidated-config-file-spec)
15. [Appendix C — legacy → unified mapping tables](#appendix-c--legacy--unified-mapping-tables)

---

## 1. Mission and scope

Build **one** Hermes Agent plugin, working name **`hermes-flaky-stabilization`**, that absorbs these
seven existing, fully implemented plugins (all checked out as siblings of this repo, all authored by
`sergiparpal`, all pure-stdlib at runtime):

| Stage | Plugin | Local checkout | Role |
|---|---|---|---|
| 1 | `hermes-test-history` | `../hermes-test-history` | Indexes JUnit XML into SQLite+FTS5; failure-history lookup tools |
| 2 | `hermes-flaky-detective` | `../hermes-flaky-detective` | Detects flaky tests from the history DB; verdict store; nightly no-agent scan |
| 3 | `hermes-ci-triage` | `../hermes-ci-triage` | Classifies a CI log into a 6-category taxonomy (LLM + heuristic fallback); learns patterns |
| 4 | `hermes-flaky-healer` | `../hermes-flaky-healer` | Diagnoses flaky Playwright tests, patches a sandboxed copy, burn-in verifies, opens a PR; recipe store |
| 5 | `hermes-bug-report-improver` | `../hermes-bug-report-improver` | LLM rewrite of a raw bug report into a structured issue |
| 6 | `hermes-masking-validator` | `../hermes-masking-validator` | Read-only PII scan of evidence files (8 validated detectors, optional OCR) |
| 7 | `hermes-jira-incidents` | `../hermes-jira-incidents` | MemoryProvider that pulls Jira incidents into a local FTS5 index; 3 redacted lookup tools |

The unified plugin must:

- Preserve **every public tool contract** (13 tools) byte-compatibly (names, schemas, JSON return
  shapes) so external consumers keep working — especially `test-history`, which is a keystone for
  plugins outside this pipeline.
- Convert the `jira-incidents` memory provider into plain (`general`) tools **without losing its
  PII egress redaction**, freeing the exclusive memory slot.
- Add the **orchestration layer that does not exist today**: a pipeline entry tool that runs
  history → detection → triage, forks on the triage verdict (flaky → healer; real bug →
  bug-report → PII gate → incident dedup → optional tracker write), and closes the two feedback
  loops the proposal described but no code implements.
- Consolidate private state into one SQLite DB with data migration; keep the public
  `test-history/history.db` untouched (it is a cross-plugin data contract).
- Enforce a coherent safety policy: sandbox-only test modification, PR-only git flow through the
  host approval pipeline, approval escalation for sensitive tools, and a PII gate before any
  external output.

Out of scope: modifying Hermes core (forbidden — §3.11), building CI-provider adapters beyond
GitHub Actions, ML-based PII detection, and any UI work.

---

## 2. Ground truth: verified corrections to the proposal

The original proposal document was a starting hypothesis. Every one of these was checked against
real code. **The implementation must follow this table, not the proposal.**

| # | Proposal claim | Verified reality |
|---|---|---|
| 1 | Hermes target "v0.14.0 'Foundation'", Python 3.11+ | Core checkout is **0.18.2**; no version codenames exist anywhere in the source. `requires-python = ">=3.11,<3.14"`. |
| 2 | Plugins have a `category` field; 7 categories; the pipeline is category `general` | The manifest field is **`kind`**, one of `standalone`, `backend`, `exclusive`, `platform`, `model-provider` (default `standalone`). None of the seven manifests declares a category/kind at all; "general" exists only in prose. The unified plugin is `kind: standalone`. |
| 3 | Plugins load automatically at startup, disabled afterwards with `hermes plugins disable` | Standalone plugins are **opt-in**: they load only if listed in `plugins.enabled` in `config.yaml` (`hermes plugins enable <name>`). `plugins.disabled` always wins. Bundled backends/platforms auto-load; user plugins do not. |
| 4 | 15 hooks in `VALID_HOOKS`; 6 with actionable returns | **23 hooks** (adds `pre_verify`, `pre_api_request`, `post_api_request`, `api_request_error`, `subagent_start`, `kanban_task_claimed/completed/blocked`). Actionable returns: `pre_tool_call` (block **and** approve-escalate), `pre_llm_call`, `transform_tool_result`, `transform_terminal_output`, `transform_llm_output`, `pre_verify`, `pre_gateway_dispatch`. |
| 5 | `pre_approval_request`/`post_approval_response` are "gating points" | **Both are observers; their return value is ignored** (docs literally say "ignored"). They cannot veto or answer an approval. The real plugin-side gate is `pre_tool_call` returning `{"action":"approve"|"block", ...}`; dispatched tools additionally pass the host approval pipeline. `flaky-healer` uses these two hooks correctly — as audit log observers only. |
| 6 | Stages call each other via `ctx.dispatch_tool` | Almost no inter-plugin dispatch exists. The **only** cross-plugin dispatch is `ci-triage`'s optional enrichment, which calls `test_failure_lookup` / `module_failure_history` — **with the wrong argument key** (`"query"` instead of `test_id`/`path`; see `hermes-ci-triage/enrichment.py:38-41`), so it receives a `{"success": false, ...}` validation envelope that its unknown-tool filter does not catch, and injects that useless envelope into the LLM prompt. This is a live bug the unified plugin fixes with a correct internal call. `flaky-detective` couples to `test-history` by reading its SQLite file **read-only** (`mode=ro` URI), not by tool dispatch. `flaky-healer` dispatches only host tools (`terminal`, `create_pull_request`). |
| 7 | `test-history` indexes JUnit XML / Allure / pytest cache | **JUnit XML only** (pytest, jest-junit, surefire dialects). Allure and pytest-cache are explicitly out of scope of v0.1.0. |
| 8 | `bug-report-improver` has `search_possible_duplicates(summary, tracker?)` and requires tracker read access | **Neither exists.** It is a single-tool (`improve_bug_report`), offline, zero-egress LLM rewriter. Duplicate search must be built new (against the local incidents index — see D9). |
| 9 | `jira-incidents` "pushes the structured, clean ticket to the tracker" | **Read-only.** Its Jira client is GET-only (`/rest/api/{v}/search/jql`, `/issue/{key}`); no POST/PUT exists. It *pulls* incidents into a local index. Tracker write-back is **net-new** work (D7). Also: redaction is **egress-only** — the local store holds unredacted rows; every model-facing path redacts. |
| 10 | `flaky-healer` "persists the procedure as an auto-generated Skill" and "feeds the fix back into test-history" | It persists recipes to its own SQLite and regenerates a read-only `learned-patterns.md` markdown mirror; the only *registered* skill is a static `SKILL.md`. **No feedback into test-history exists** — the loop is internal (recipe short-circuit). The unified orchestrator implements the real loop (D9). |
| 11 | `jira-incidents` incident context "feeds back into ci-triage" | **No such mechanism exists** in either plugin. Built new in the unified orchestrator (D9). |
| 12 | Cron is a plugin capability (manifest/ctx) | **No `ctx.register_cron` and no manifest cron field exist.** `flaky-detective` creates its job imperatively: `hermes cron create "0 9 * * *" --no-agent --script flaky-scan.sh ...` from an `install-cron` CLI subcommand. The unified plugin reproduces that pattern (D10). |
| 13 | `pre_llm_call` injections concatenate alphabetically by plugin folder | Concatenated with `"\n\n"` in **registration order** = discovery order (bundled → user → project → entry-point; alphabetical within each source). Oversized pieces (>10k chars) spill to `$HERMES_HOME/hook_outputs/`. |
| 14 | Tool signature details (`test_failure_lookup(test_id)`, `fetch_ci_logs(build_id)`, `validate_no_pii(file_or_dir)`, …) | Several params were missing or wrong. The exact verified schemas are in §4 and are the binding contract. Notably `fetch_ci_logs` requires **both** `build_id` and `repo`; `validate_no_pii`'s param is **`target`**; `improve_bug_report` also takes `context` and `format`. |
| 15 | "healer modifies the test in an isolated container" | It patches a **temporary copy** of the project, never the original tree, inside a hardened Docker sandbox (default) **or** a weaker subprocess fallback (`isolation:"subprocess"`, which refuses PR mode unless explicitly allowed). Preserve both backends and the refusal guard. |
| 16 | memory category exclusivity | Confirmed: one external memory provider at a time, selected via `memory.provider` in `config.yaml`, enforced by `MemoryManager`. But note: memory providers do **not** register through the normal `PluginContext` — the memory loader passes a collector object exposing `register_memory_provider`. Folding jira-incidents into a standalone plugin therefore *cannot* keep the provider class registered; its lifecycle value must be re-provided via hooks (D1). |

---

## 3. Hermes plugin platform reference

Everything the implementer needs about the host, verified against `/home/sergi/hermes-agent`
(`hermes_cli/plugins.py`, `agent/plugin_llm.py`, `agent/memory_provider.py`, `cron/jobs.py`,
`website/docs/`). Core version 0.18.2.

### 3.1 What makes a directory a plugin; discovery; enablement

- A plugin is a directory containing **`plugin.yaml`** (or `.yml`) **and `__init__.py` exporting
  `register(ctx)`**. A missing `register()` records the plugin with an error; a `register()` that
  raises disables only that plugin (agent continues).
- Discovery sources, in order (later overrides earlier on key collision): bundled
  (`<hermes-agent>/plugins/`), **user (`~/.hermes/plugins/<name>/`)** ← where this plugin installs,
  project (`./.hermes/plugins/`, only when `HERMES_ENABLE_PROJECT_PLUGINS` is truthy), and pip
  entry points (group `hermes_agent.plugins`).
- Each directory plugin is imported as module **`hermes_plugins.<slug>`** (slug: `-`→`_`, `/`→`__`)
  with `submodule_search_locations` set, so **relative imports inside the package work** even
  though the directory name is hyphenated.
- **Enablement is opt-in**: a standalone plugin loads only if its key or bare name is in
  `plugins.enabled` (config.yaml). `plugins.disabled` always wins. `hermes plugins
  enable|disable|list|install|update|remove` manage this. `HERMES_SAFE_MODE=1` skips all plugin
  loading; `HERMES_PLUGINS_DEBUG=1` prints verbose discovery logs to stderr.
- `hermes plugins install <owner>/<repo>` git-clones into `~/.hermes/plugins/`, prompts for
  `requires_env` values (rich entries support `description`/`url`/`secret`), copies `*.example`
  files, renders `after-install.md` if present, and offers to enable.

### 3.2 `plugin.yaml` manifest fields (all optional; unknown keys ignored)

| Field | Type | Meaning |
|---|---|---|
| `name` | str | Defaults to directory name. Used for `plugins.enabled` matching and the skill namespace. |
| `version` | str | Display only. |
| `description` | str | Shown in `hermes plugins list`. |
| `author` | str | Display only. |
| `kind` | str | `standalone` (default) / `backend` / `exclusive` (memory) / `platform` / `model-provider`. **Auto-detection warning:** if `kind` is omitted, the loader scans the first 8192 bytes of `__init__.py` and coerces to `exclusive` if it sees the strings `register_memory_provider` or `MemoryProvider` — so the unified plugin's `__init__.py` must not contain those strings, and must set `kind: standalone` explicitly anyway. |
| `requires_env` | list[str \| {name, description, url, secret}] | Env vars the plugin needs. **A plugin with unset `requires_env` vars is disabled entirely** with a message — which is why plugins with *optional* credentials (ci-triage, flaky-healer) deliberately omit it and gate per-tool with `check_fn`. Do the same. |
| `provides_tools`, `provides_hooks` | list[str] | Documentation/declarative only; real registration happens in `register()`. Keep them accurate. |
| `manifest_version` | int | Read by the installer only; current supported value is 1. |

### 3.3 `PluginContext` API (the methods this plugin uses)

Exact signatures from `hermes_cli/plugins.py`:

```python
register_tool(name: str, toolset: str, schema: dict, handler: Callable,
              check_fn: Callable | None = None, requires_env: list | None = None,
              is_async: bool = False, description: str = "", emoji: str = "",
              override: bool = False) -> None
register_hook(hook_name: str, callback: Callable) -> None
register_command(name: str, handler: Callable, description: str = "", args_hint: str = "") -> None
register_cli_command(name: str, help: str, setup_fn: Callable,
                     handler_fn: Callable | None = None, description: str = "") -> None
register_skill(name: str, path: Path, description: str = "") -> None
dispatch_tool(tool_name: str, args: dict, **kwargs) -> str
```

- `schema` is OpenAI function-calling style: top-level `name`, `description`, `parameters`
  (`{"type":"object","properties":{...},"required":[...]}`). The `description` kwarg on
  `register_tool` is additional display metadata; the model sees the schema.
- **Tool handler contract:** `def handler(args: dict, **kwargs) -> str` — always return a JSON
  string (success and error), never raise, always accept `**kwargs`.
- `check_fn` returning falsy hides the tool from the model (used for optional credentials).
- Tool **name collisions**: a name already claimed by a *different* toolset is rejected unless
  `override=True` (which needs a per-plugin trust grant for built-ins). Practical consequence:
  the legacy seven plugins must be disabled before enabling the unified one (§10).
- `register_command` handler: `fn(raw_args: str) -> str | None`, sync or async; rejected if it
  collides with a built-in command (wrap registration in try/except like the legacy plugins do).
- `register_skill(name, path)`: name must match `[a-zA-Z0-9_-]+`, no `:`; registered under the
  qualified name **`<manifest name>:<skill name>`**; read-only; resolvable only by explicit
  `skill_view("plugin:skill")`, not listed in the system prompt index.
- `dispatch_tool` goes through the **full approval / redaction / budget pipeline** — a real tool
  invocation. Use it for host tools (`terminal`, `create_pull_request`); use direct function calls
  for our own internal stages.
- `ctx.profile_name` is the safe way to identify the profile; `ctx._cli_ref` is `None` outside the
  interactive CLI — never rely on it.
- There is **no** `ctx.register_memory_provider` on the real `PluginContext` (only the memory
  loader's collector has it) and **no cron registration method**.

### 3.4 `ctx.llm` (`agent/plugin_llm.py::PluginLlm`)

```python
complete(messages, *, provider=None, model=None, temperature=None, max_tokens=None,
         timeout=None, agent_id=None, profile=None, purpose=None)
complete_structured(*, instructions: str, input: Sequence[dict], json_schema=None,
         json_mode=False, schema_name=None, system_prompt=None, provider=None, model=None,
         temperature=None, max_tokens=None, timeout=None, agent_id=None, profile=None,
         purpose=None)
acomplete(...), acomplete_structured(...)   # async twins
```

- `input` blocks: `{"type":"text","text":...}` or `{"type":"image","data":bytes|"url":...}`.
- Structured results expose `.parsed` (validated JSON or None), `.text`, `.content_type`
  (`"json"|"text"`), `.usage`. Vision inputs auto-route to the configured vision model; the plugin
  decides nothing about models/credentials. `provider=`/`model=` overrides are trust-gated per
  plugin; request-shaping args (`temperature`, `max_tokens`, `timeout`, schemas, `purpose`) are
  always allowed. Three legacy stages already use exactly `complete_structured` with keyword args —
  keep their call sites unchanged.

### 3.5 Hooks (`VALID_HOOKS`, 23 total) and which returns matter

Dispatch: all callbacks fire, each in its own try/except — **exceptions are logged at WARNING and
swallowed**; a broken hook never breaks the agent. Callbacks must accept `**kwargs`.

Hooks with consumed return values (the rest are observers):

| Hook | Callback receives (main kwargs) | Actionable return |
|---|---|---|
| `pre_tool_call` | `tool_name, args, task_id, session_id, ...` | `{"action":"block","message":...}` vetoes (message becomes the tool result); `{"action":"approve","message":...,"rule_key":...}` escalates to the human approval gate, **fail-closed** (error/deny/timeout ⇒ blocked). First valid directive wins. |
| `pre_llm_call` | `session_id, user_message, conversation_history, is_first_turn, model, ...` | `{"context": str}` or a plain non-empty string injects text into the **user message** (never the system prompt); multiple plugins' pieces are joined with `"\n\n"` in registration order; >10k-char pieces spill to files. |
| `transform_tool_result` | `tool_name, arguments, result, task_id, ...` | `str` replaces the tool result the model sees; `None` = unchanged. |
| `transform_terminal_output` | `command, output, exit_code, cwd, ...` | `str` replaces raw terminal output pre-redaction. |
| `transform_llm_output` | `response_text, session_id, model, platform, ...` | First non-empty `str` replaces the final response. |
| `pre_verify` | `session_id, final_response, changed_paths, attempt, ...` | `{"action":"continue","message":...}` keeps the agent going (bounded by `agent.max_verify_nudges`). |
| `pre_gateway_dispatch` | `event, gateway, session_store` | `{"action":"skip"|"rewrite"|"allow", ...}`. |

Observers used by this plugin: `pre_approval_request` (`command, description, pattern_key,
pattern_keys, session_key, surface`) and `post_approval_response` (adds
`choice ∈ once|session|always|deny|timeout`) — **returns ignored** — plus `on_session_start`.

### 3.6 Cron (no plugin registration surface)

Jobs are created via the `hermes cron` CLI or programmatically with `cron/jobs.py:create_job`.
Schedule strings (`cron/jobs.py:parse_schedule`): `"30m"`/`"2h"` (once), `"every 30m"` (interval),
5–6-field cron expressions (validated with `croniter`), ISO timestamps (once). `--no-agent
--script <shim>` jobs run a shell script with zero LLM cost; script shims live in
`$HERMES_HOME/scripts/`. This plan reuses flaky-detective's proven pattern: an `install-cron` CLI
subcommand that writes config, installs the shim, and shells `hermes cron create` (printing the
copy-paste command if the CLI is unavailable).

### 3.7 Persistence conventions

No ctx-provided data dir. Convention from bundled plugins: resolve
`hermes_constants.get_hermes_home()` (fall back to `$HERMES_HOME`, then `~/.hermes`) and keep
state under it. SQLite: WAL journal mode (with fallback for network filesystems),
`timeout` on connect, `mkdir(parents=True, exist_ok=True)`, restrictive permissions (dirs `0700`,
DB files `0600` — all seven legacy plugins already do this; keep it).

### 3.8 Approvals

- Dangerous terminal patterns are gated by the host inside the `terminal` tool; anything a
  `pre_tool_call` hook flags with `{"action":"approve"}` also goes to the human gate (fail-closed).
- `ctx.dispatch_tool` runs the full pipeline, so the healer's git/PR steps inherit host approvals
  without any plugin code.
- `pre_approval_request`/`post_approval_response` are for observation/audit only.

### 3.9 Testing conventions (core-side)

Core tests build real objects: `PluginManager()`, `PluginManifest(name=..., source="user")`,
`PluginContext(manifest, pm)`, then swap the singleton with
`monkeypatch.setattr(hermes_cli.plugins, "_plugin_manager", pm)` and assert on `pm._hooks`,
`pm._plugin_skills`, `pm._cli_commands`, the global `tools.registry`. `jira-incidents`' test suite
shows the pattern for out-of-tree plugins: locate a hermes-agent checkout via `HERMES_REPO` env
(fallbacks to `~/hermes-agent` etc.), put it on `sys.path`, and skip the integration tests cleanly
when absent. Adopt that.

### 3.10 Python and dependencies

Target **Python ≥ 3.11, < 3.14** (match core; two legacy READMEs say "3.12+" but their code runs
on 3.11 — verify with the ported suites in CI). Runtime dependencies: **standard library only**
(all seven legacy plugins are stdlib-only). Optional extras: `pytesseract>=0.3.10` +
`pillow>=11.3.0` + the `tesseract` binary for OCR (runtime-probed, never imported at module load).
Dev/test: `pytest`, optionally `pytest-cov`, `ruff`.

### 3.11 The never-modify-core rule

From CONTRIBUTING.md and the build-a-plugin doc: register everything through the exposed surface —
*"if your plugin needs a capability the framework doesn't expose, that's a feature request to widen
the generic plugin surface (a new hook or `ctx` method) — never special-case your plugin in core."*
This plan requires zero core changes.

---

## 4. Source plugin inventory

The binding public contract. Porting phases copy schemas/handlers verbatim from the listed source
files; this section is the acceptance reference. All handlers return **JSON strings**.

### 4.1 `hermes-test-history` (keystone — contract frozen)

- Source: `../hermes-test-history/` (`__init__.py`, `schema.py`, `storage.py`, `queries.py`,
  `parser.py`, `ingest.py`, `cli.py`, `domain.py`, `timeutil.py`; ~1.2k LOC; GPLv3).
- Tools (toolset `test_history`):
  - **`test_failure_lookup`** — params: `test_id` (str, required; bare name, `classname::name`,
    `file_path::name`, or FTS query), `limit` (int, default 10, min 1, max 50). Returns
    `{success, content_warning, test_id, matched_tests, total_runs, failure_count,
    last_failure_at, failures:[{run_id, timestamp, status, classname, name, file_path,
    failure_type, message, stack_trace_excerpt}]}`. Errors:
    `{success:false, error, remediation}`; internal errors never leak exception text.
  - **`module_failure_history`** — params: `path` (str, required; prefix match on
    `test_cases.file_path`, rejects `..`), `since` (ISO-8601, default 30 days back),
    `min_failures` (int, default 1). Returns `{path, window_start, tests_with_failures,
    top_offenders:[{name, file_path, classname, failure_count, total_runs, last_failure_at}],
    truncated}` (top 50).
- CLI `test-history`: subcommands `ingest <path>`, `status`, `prune --before <ISO>`,
  `rebuild-fts`, `config`.
- DB (**public data contract — do not move or alter**): `<hermes_home>/test-history/history.db`,
  WAL, PRAGMAs `foreign_keys=ON, recursive_triggers=ON, case_sensitive_like=ON`. Tables
  `test_runs`, `test_cases`, external-content FTS5 `test_cases_fts` (columns `classname, name,
  failure_message, stack_trace`) + 3 sync triggers, `schema_version` (pinned 1). Ingestion is
  JUnit-XML-only via a hardened raw-expat parser (DTD/DOCTYPE rejected, 64 MiB cap) — **do not
  "simplify" to `ET.parse`**. Config `<hermes_home>/test-history/config.json`
  (`default_lookback_days=30`, `max_stack_trace_chars=500`, `db_path_override` constrained inside
  hermes home).
- No hooks, no LLM, no dispatch_tool, no third-party deps.

### 4.2 `hermes-flaky-detective`

- Source: `../hermes-flaky-detective/` (`detect.py`, `query.py`, `scan.py`, `storage.py`,
  `schema.py`, `config.py`, `reporting.py`, `cli.py`, `domain.py`, `timeutil.py`,
  `flaky-scan.sh`; ~1.9k LOC).
- Tool (toolset `flaky_detective`): **`is_flaky`** — param `test_id` (str, required). Returns
  `{success, content_warning, test_id, test_key, is_flaky, status, fails, passes, runs,
  window_days, last_failure, computed_at[, matched, note]}`, or an `unknown` verdict, or the
  standard error envelope.
- Detection (pure function `detect.compute_verdicts`): within `window_days` (default **14**),
  `flaky ⇔ fails ≥ min_fails (default 3) AND passes ≥ 1`;
  `consistently_failing ⇔ fails ≥ min_fails AND passes = 0`; else `stable`. `include_errors`
  (default true) counts `error` as fail. Reads `history.db` **read-only** (`file:...?mode=ro`),
  dedupes logical runs by `(source_file, effective_ts)`, pins source `schema_version==1`
  (warn-only). Empty windows preserve the previous verdict snapshot.
- Own DB: `<hermes_home>/flaky-detective/verdicts.db` — tables `flaky_verdicts` (PK `test_key`;
  wholesale-replaced per scan), `scan_runs` (append-only), `schema_version`. → migrates into
  `state.db` (Appendix A).
- CLI `flaky-detective`: `scan [--window --min-fails --include-errors --format human|cron|json]`,
  `status`, `list [--status]`, `install-cron [--schedule --deliver --window --min-fails
  --no-create]`. Cron: default `"0 9 * * *"`, no-agent shim `flaky-scan.sh` →
  `hermes flaky-detective scan --format cron` (empty stdout = silent tick).
- No hooks, no LLM.

### 4.3 `hermes-ci-triage`

- Source: `../hermes-ci-triage/` (`handlers.py`, `classifier.py`, `taxonomy.py`, `prefilter.py`,
  `redact.py`, `logfetch.py`, `safehttp.py`, `patterns.py`, `enrichment.py`, `ports.py`; ~2.0k
  LOC). Strict ports-and-adapters: only `__init__.py` touches Hermes; `llm`, `dispatch_tool`,
  `hermes_home` are injected kwargs — ideal for embedding.
- Tool (toolset `ci_triage`): **`triage_pipeline_failure`** — params: `log_url_or_path` (str,
  required; local path or https URL), `project` (str, optional; defaults to cwd basename).
  Returns `{success, category, confidence, summary, evidence[], suggested_action,
  classification_method:"llm"|"heuristic", prior_seen, prior_occurrences, project, signature,
  log_stats:{original_bytes, hit_count, truncated, low_signal}[, prior_match:"fuzzy",
  enrichment]}`.
- Taxonomy (single source `taxonomy.CATEGORIES`): `broken_test, environment, data, timeout,
  flaky, infra`; default `broken_test`. Classification: `ctx.llm.complete_structured`
  (temperature 0.0, max_tokens 700, timeout 60, enum-constrained schema) with deterministic
  heuristic fallback (priority-ordered rules, confidence 0.3/0.1). Prompt hardened against
  injection (excerpt fenced as UNTRUSTED).
- Pipeline invariant to preserve: **redact (`redact.redact`) before hash / store / LLM /
  enrichment / echo** — single chokepoint in `handlers`.
- Pattern store: `$HERMES_HOME/cache/ci_triage_patterns.db` — table `patterns` (PK
  `(project, signature)`; SHA-1 of volatility-normalized excerpt), FTS5 `patterns_fts` when
  available, self-pruning (180 days / 500 rows per project). → migrates into `state.db`.
- Remote fetch: GitHub-only (`GITHUB_TOKEN` read at call time — **no** `requires_env`), heavy SSRF
  hardening in `safehttp.py` (per-hop redirect re-validation, Authorization stripped cross-host,
  DNS-rebind guard, private ranges blocked unless `HERMES_CI_TRIAGE_ALLOW_PRIVATE`); local log
  roots allowlist `HERMES_CI_TRIAGE_LOG_ROOTS`; 25 MB cap. **Keep all of it.**
- Enrichment bug to fix on port (§2 row 6): call the history query directly
  (`queries.test_failure_lookup(conn, test_id=<signal line>, ...)` — the FTS fallback makes
  free-text queries work) instead of dispatching with the wrong arg key.

### 4.4 `hermes-flaky-healer` (largest stage)

- Source: `../hermes-flaky-healer/` (`handlers.py`, `schemas.py`, `flaky_healer/{healer, diagnose,
  trace, gitflow, config, logfilter, zipsafe, skill_export}.py`, `flaky_healer/ci/{base,
  github_actions}.py`, `flaky_healer/sandbox/{base, docker, subproc}.py`,
  `flaky_healer/strategies/*`, `flaky_healer/recipes/{store, signature, matcher, shapes}.py`,
  `skills/flaky-healer/SKILL.md`; ~2.8k LOC; Python 3.11+ stdlib).
- Tools (toolset `flaky_healer`):
  - **`fetch_ci_logs`** — params `build_id` (str, required), `repo` (str `owner/name`, required);
    **`check_fn`: hidden unless `GITHUB_TOKEN` is set.** Returns `{run:{id,name,status,conclusion,
    head_branch,head_sha,html_url}, failed_jobs:[{id,name,conclusion,failed_steps}], filtered_log,
    bytes_raw, bytes_filtered, anchor_count, note}`.
  - **`analyze_playwright_trace`** — param `trace_path` (str, required; also accepts undocumented
    `log_excerpt`). Returns `{diagnosis:{cause, triage_label, evidence, failing_action, selector,
    recommended_strategy, confidence, ...}, trace:<brief>}`. Parses Playwright trace format v8
    (zip-bomb-guarded).
  - **`heal_flaky_test`** — params `repo_dir` (req), `test_id` (req, spec path), `trace_path?`,
    `build_id?`, `repo?`, `mode` (`suggest`|`pr`, default `suggest`), `strategy`
    (`bump_timeout`|`testid_selector`|`await_state`). Flow: recipe short-circuit (signature
    exact→relaxed match) → two-stage diagnosis (heuristics; LLM only when confidence < 0.7) →
    strategy plan → reproduce M runs on an **unpatched temp copy** → patch copy + burn-in N runs
    (default `5:10`; stable ⇔ N/N green) → recipe upsert + `learned-patterns.md` regeneration.
    PR mode: refuses on error/unstable/empty diff/subprocess isolation (unless
    `FLAKY_HEALER_ALLOW_SUBPROCESS_PR=1`); otherwise dispatches, via `ctx.dispatch_tool`, the host
    tools: `git checkout -b fix/flaky-<slug>-<sig8>` → `git apply --index` → `git commit` →
    `git push -u origin <branch>` → `create_pull_request`. Pushing to default/protected branches
    is structurally impossible (branch-name guards). Returns `{mode, report:{...}, warnings?,
    pr?, pr_skipped?}`.
  - **`list_healing_recipes`** — no params. Returns `{count, recipes[], db_path,
    learned_patterns_md}`.
- Hooks: `pre_approval_request`, `post_approval_response` — **audit observers** writing redacted
  events (key-name + token-shape masking) to the `audit` table. Keep as-is.
- Skill: static `skills/flaky-healer/SKILL.md` via `register_skill("flaky-healer", ...)`.
  Slash command: `/heal <repo_dir> <test_id> [suggest|pr]`.
- Sandbox: Docker (digest-pinned `mcr.microsoft.com/playwright:v1.60.0-noble@sha256:9bd26ad9…`,
  `--network=none --cap-drop=ALL --read-only ...`) or subprocess fallback (fresh env, rlimits,
  best-effort net-namespace). Config via `FLAKY_HEALER_*` env vars (see Appendix B mapping).
- Own DB: `<data_dir>/healer.db` — `runs`, `recipes` (SCHEMA_VERSION 2 with a versioned migration
  ladder — **adopt this migration pattern for `state.db`**), `audit`. → migrates into `state.db`.
- No cross-plugin dispatch; `TRIAGE_FOR_CAUSE` maps diagnosis causes onto the ci-triage taxonomy
  (data-level composability only).

### 4.5 `hermes-bug-report-improver`

- Source: `../hermes-bug-report-improver/hermes_bug_report_improver/` (`schema.py`, `handler.py`,
  `engine.py`, `prompts.py`, `domain.py`, `rendering.py`, `validation.py`, `host.py`; ~0.9k LOC;
  root-shim `__init__.py` pattern — reuse it for the unified repo).
- Tool (toolset `qa`): **`improve_bug_report`** — params `raw_text` (str, required, ≤16 KiB),
  `context` (str, optional, ≤4 KiB), `format` (`markdown`|`json`, default `markdown`). Success:
  markdown string, or JSON with exactly
  `{title, summary, reproduction_steps[], expected_behavior, actual_behavior, severity,
  severity_rationale, missing_evidence[]}` (`additionalProperties:false`; severity ∈
  critical/high/medium/low/unknown). Error: `{"error": "..."}` JSON. One
  `ctx.llm.complete_structured` call (temperature 0.0, 2048 tokens, one retry at 4096 with a
  stricter suffix), 3 few-shot examples, "never invent facts" rules.
- Slash command `/improve-bug` (registration wrapped in try/except).
- Markdown output passes `rendering._sanitize_for_md` (ANSI/bidi strip, HTML-escape, leading
  block-marker escaping) — keep; JSON output stays byte-faithful.
- No persistence, no network, no tracker, no env vars.

### 4.6 `hermes-masking-validator`

- Source: `../hermes-masking-validator/` (`detectors.py` 538 LOC, `scanner.py` 520 LOC,
  `__init__.py`).
- Tool (toolset `qa_masking`): **`validate_no_pii`** — params `target` (str, required; file or
  dir), `types` (array, optional; enum injected from `DETECTOR_NAMES`), `max_files` (int,
  optional; default 2000, hard ceiling 10000 — callers may lower, never raise). Returns
  `{success, clean, complete, scanned_files, findings:[{file, line, type, preview}], skipped:
  [{file, reason}], truncated[, summary:{type:count}]}`. **Gate semantics: safe ⇔
  `clean && complete`.** Never returns raw PII (masked previews only, even filenames).
- Detectors (registry-driven, priority-unique, checksum-validated where possible): `email`,
  `credit_card` (Luhn), `iban` (mod-97), `spanish_dni_nie` (mod-23), `phone`, `us_ssn`,
  `us_itin`, `passport` (context-gated). Read-only (enforced by test), bounded (2 MB/file,
  1000 findings, 300 s wall clock), OCR optional via runtime-probed pytesseract/Pillow
  (`skipped: ocr_unavailable` when absent).
- Passive report tool — no hooks, gates nothing by itself; the orchestrator enforces the gate (D6).

### 4.7 `hermes-jira-incidents`

- Source: `../hermes-jira-incidents/hermes-jira-incidents/` (`__init__.py` provider 510 LOC,
  `store.py` 499, `ingest.py` 356, `redaction.py` 349, `config.py` 275, `jira_client.py` 183,
  `prefetch.py`, `sync.py`, `cli.py`; ~2.7k LOC).
- Today: a real `MemoryProvider` (registered via the memory loader's collector). To be dropped —
  the underlying modules are Hermes-free and survive intact.
- Tools (become toolset `jira_incidents` in the unified plugin; schemas preserved):
  - **`jira_search_incident`** — `query` (req), `limit` (default 5, clamp 1–50) →
    `{results:[{key, summary, status}], count}` (redacted).
  - **`jira_get_root_cause`** — `incident_key` (req) → `{incident_key, found, summary, status,
    root_cause}` or `{incident_key, found:false}` (redacted).
  - **`jira_link_session`** — `incident_key` (req), `note?` → `{status:"linked", incident_key,
    note, link_id}`; writes only to the local `links` table.
- Jira client: **GET-only** (`/rest/api/{v}/search/jql` token-paginated; `/issue/{key}`), https
  enforced, token only from env **`JIRA_API_TOKEN`** (Basic with `jira_email`, or Bearer for
  `auth_mode: oauth`), JQL default `project = INC ORDER BY updated DESC` + incremental watermark;
  backfill resumes across syncs. Errors never leak response bodies.
- Store: `incidents` (full **unredacted** rows), standalone FTS5 `incidents_fts`, `links`, `meta`
  (watermark/backfill), retention pruning. → migrates into `state.db`.
- **Redaction is egress-only and mandatory** (`redaction.py`): ordered patterns (URL credentials,
  Atlassian/AWS/Bearer/PEM secrets, labelled secrets, emails, SSN, cards, IPv4, phones, labelled
  names), known-name scrubbing with a stopword guard, prompt-injection neutralisation
  (`neutralize_untrusted`: control chars + line-leading role markers), 200k char bound, never
  raises. Optional strict canary via `HERMES_JIRA_STRICT_REDACTION`. Shares **no** code with
  masking-validator (different jobs: egress rewrite vs. evidence report) — port both, keep them
  distinct modules under one `pii/` package.
- Lost with the provider wrapper (must be re-provided): per-turn `prefetch()` injection →
  replaced by a `pre_llm_call` hook (D1); `system_prompt_block()` → dropped (tool descriptions
  cover discovery); `sync_turn`/`initialize` scheduling → `on_session_start` trigger + syncs
  before incident reads + optional cron; config schema/save → unified config (Appendix B).

---

## 5. Resolved design decisions

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

---

## 6. Target architecture

### 6.1 Repository / package layout

```
hermes-flaky-stabilization/
├── plugin.yaml                      # manifest (Appendix B)
├── __init__.py                      # root shim: sys.path insert + re-export register()
├── hermes_flaky_stabilization/
│   ├── __init__.py                  # register(ctx) — the ONLY Hermes-aware entry
│   ├── registration.py              # tool/hook/command/skill wiring, check_fns
│   ├── config.py                    # unified config load/merge/coerce (Appendix B)
│   ├── paths.py                     # hermes_home resolution, data dirs, permissions
│   ├── storage/
│   │   ├── state.py                 # state.db connection + versioned migrations (Appendix A)
│   │   └── migrate_legacy.py        # copy data from the 4 legacy DBs
│   ├── history/                     # ← hermes-test-history modules, verbatim port
│   ├── detective/                   # ← hermes-flaky-detective (query points at history.db;
│   │                                #    verdicts/scan_runs live in state.db)
│   ├── triage/                      # ← hermes-ci-triage (patterns in state.db; enrichment fixed)
│   ├── healer/                      # ← hermes-flaky-healer incl. sandbox/, strategies/,
│   │                                #    recipes/ (runs/recipes/audit in state.db), ci/
│   ├── bugreport/                   # ← hermes-bug-report-improver package modules
│   ├── pii/
│   │   ├── detectors.py, scanner.py # ← hermes-masking-validator (evidence report)
│   │   └── redaction.py             # ← hermes-jira-incidents/redaction.py (egress rewrite)
│   ├── incidents/                   # ← jira_client, ingest, store adapter, prefetch, sync
│   │   └── write.py                 # NET-NEW: jira_create_incident (D7)
│   ├── orchestrator/
│   │   ├── pipeline.py              # stabilize_test_failure control flow (D9)
│   │   ├── dedup.py                 # NET-NEW: find_duplicate_incidents
│   │   └── hooks.py                 # pre_tool_call escalation, pre_llm_call injection,
│   │                                #   on_session_start sync trigger, approval audit observers
│   └── cli.py                       # `flaky-stab` CLI + `test-history` alias
├── skills/flaky-healer/SKILL.md     # ported static skill
├── scripts/
│   ├── run_tests.sh                 # CI-parity wrapper (TZ=UTC, cred-env scrub — legacy pattern)
│   └── flaky-stab-scan.sh           # cron shim
├── tests/                           # ported + new suites (§8)
├── pyproject.toml                   # requires-python ">=3.11,<3.14"; dev extras pytest/ruff
├── requirements-ocr.txt             # optional: pytesseract>=0.3.10, pillow>=11.3.0
├── README.md, MIGRATION.md, after-install.md, LICENSE (GPL-3.0-only)
```

Porting rule: module contents are copied **verbatim where possible**; the only sanctioned edits
are (a) import paths (`from . import x` → package-relative under the new tree), (b) storage
adapters pointed at `state.db`, (c) the enrichment fix, (d) the healer data-dir default. Each
deviation must be listed in the phase's commit message.

### 6.2 Public surface (complete)

**Tools — 13 preserved + 3 new.** Preserved tools keep their original toolset names (maximizes
compatibility with per-toolset gating and avoids retraining users):

| Tool | Toolset | Status | check_fn |
|---|---|---|---|
| `test_failure_lookup`, `module_failure_history` | `test_history` | preserved verbatim | — |
| `is_flaky` | `flaky_detective` | preserved verbatim | — |
| `triage_pipeline_failure` | `ci_triage` | preserved (enrichment fixed internally) | — |
| `fetch_ci_logs` | `flaky_healer` | preserved | `GITHUB_TOKEN` present |
| `analyze_playwright_trace`, `heal_flaky_test`, `list_healing_recipes` | `flaky_healer` | preserved | — |
| `improve_bug_report` | `qa` | preserved | — |
| `validate_no_pii` | `qa_masking` | preserved | — |
| `jira_search_incident`, `jira_get_root_cause`, `jira_link_session` | `jira_incidents` | preserved schemas; now plain tools | `JIRA_API_TOKEN` present **or** local index non-empty (reads should work offline once synced) |
| `stabilize_test_failure` | `flaky_stabilization` | **new** (D9) | — |
| `find_duplicate_incidents` | `flaky_stabilization` | **new** (D9): params `summary` (req), `limit` (default 5) | — |
| `jira_create_incident` | `jira_incidents` | **new** (D7) | `JIRA_API_TOKEN` present AND `jira.enable_write` |

**Hooks registered:** `pre_tool_call` (approval escalation, D6.3), `pre_llm_call` (incident
context, D1), `on_session_start` (debounced background Jira sync trigger),
`pre_approval_request` + `post_approval_response` (audit observers → `audit` table).
`provides_hooks` in the manifest lists exactly these five.

**Slash commands:** `/heal`, `/improve-bug` (preserved), `/stabilize` (new). All registrations
wrapped in try/except (built-in-collision tolerance, legacy pattern).

**CLI:** `hermes flaky-stab <subcommand>` — `status`, `ingest <path>`, `prune --before`,
`rebuild-fts`, `scan [...]`, `list [--status]`, `install-cron [...]`, `jira sync|status`,
`migrate [--dry-run]`, `config`. Plus alias command `test-history` re-exposing
`ingest/status/prune/rebuild-fts/config` unchanged (D2).

**Skill:** `flaky-healer` (qualified name becomes `hermes-flaky-stabilization:flaky-healer`; the
old qualifier `hermes-flaky-healer:flaky-healer` disappears — documented breaking change in
MIGRATION.md).

### 6.3 Orchestrator control flow (normative)

```
stabilize_test_failure(args: {log_url_or_path? , test_id?, repo_dir?, trace_path?,
                              build_id?, repo?, project?, mode?})
  1. Evidence: if log given → triage prefilter path; if build_id+repo & GITHUB_TOKEN → fetch_ci_logs.
  2. History: test_id → history.test_failure_lookup + detective.is_flaky (direct calls).
  3. Triage: triage.classify with (a) fixed history enrichment, (b) NEW incident enrichment
     (incidents.store.search(top_signal_line) → redact → prior-hint block).
  4. FORK on category:
     a. flaky | timeout  → healer.heal(mode per args/config; pr requires approval via hook D6.3)
         └─ if report.stable → history.ingest_synthetic_run(burn-in outcome)   # loop 1
     b. else → bugreport.build_report(raw evidence)
         → dedup.find_duplicate_incidents(title+summary)
         → PII gate: validate_no_pii on evidence paths (clean && complete) + redact_text on fields
         → jira.enable_write ? incidents.write.create_incident(...) : return ticket body
  5. Record pipeline_runs row; return one JSON envelope:
     {success, stage_results:{history, detective, triage, healer|bugreport, dedup, pii, tracker},
      outcome: "healed"|"pr_opened"|"ticket_created"|"ticket_ready"|"needs_attention", notes[]}
```

Every stage result is included even when skipped (`{"skipped": reason}`) — the model and tests can
always see why a branch was not taken.

---

## 7. Implementation phases

Rules for the implementing agent:

- Phases are strictly ordered; **do not start phase N+1 until phase N's acceptance criteria all
  pass** (run them yourself — no human sign-off between phases).
- The **only** human interaction is the single batched question set in Phase 0. Defaults are
  specified; if the user simply confirms, proceed immediately.
- Never modify files under `/home/sergi/hermes-agent` or the seven legacy repos (read-only
  sources).
- After each phase: run `scripts/run_tests.sh`, then commit with a message listing any sanctioned
  deviations (§6.1 porting rule).

### Phase 0 — Preflight and the one user checkpoint

**Tasks**
1. Verify the environment: the seven sibling repos and `/home/sergi/hermes-agent` exist; Python
   ≥ 3.11 available; `pytest` importable. Write `scripts/preflight.sh` that checks all of this and
   exits non-zero with a named missing item.
2. Ask the user ONE batched question set (Claude Code CLI), then proceed with the answers or the
   defaults:
   - Q1 Plugin name stays `hermes-flaky-stabilization`? **Default: yes.**
   - Q2 Include the net-new `jira_create_incident` write tool (off-by-default at runtime)?
     **Default: yes (build it, ship disabled).**
   - Q3 License GPL-3.0-only for the unified repo (forced by absorbed GPL code)? **Default: yes.**
   - Q4 Keep the `test-history` CLI alias? **Default: yes.**

**Acceptance criteria**
- `bash scripts/preflight.sh` exits 0.
- Answers (or defaults) recorded in `docs/DECISIONS.md`.

### Phase 1 — Scaffolding, manifest, config, storage core, test harness

**Files:** `plugin.yaml`, root `__init__.py` shim, `hermes_flaky_stabilization/{__init__,
registration, config, paths}.py`, `storage/state.py`, `pyproject.toml`, `scripts/run_tests.sh`,
`tests/conftest.py`, `tests/test_register.py`, `tests/test_storage_state.py`,
`tests/test_config.py`.

**Tasks**
1. Manifest per Appendix B; root shim copied from bug-report-improver's pattern (sys.path insert +
   re-export), including its subprocess loader-parity test.
2. `paths.py`: hermes-home resolution (try `hermes_constants.get_hermes_home()` /
   `hermes_cli.utils.display_hermes_home`, fall back `$HERMES_HOME`, then `~/.hermes`), data dir
   `<home>/flaky-stabilization/` with `0700`/`0600` permissions.
3. `storage/state.py`: connection factory (WAL, `check_same_thread=False`, `timeout=15`,
   row_factory), `SCHEMA_VERSION = 1`, Appendix A DDL as migration step 1, healer-style ladder
   (`current_version()`, ordered idempotent steps).
4. `config.py`: load/merge/coerce Appendix B defaults; malformed file degrades to defaults (never
   raises); env-var overrides honored; `write_config` atomic + `0600`.
5. `registration.py` + `__init__.register(ctx)`: skeleton that registers a placeholder `status`
   CLI and nothing else yet; heavy imports deferred inside `register()` (legacy discipline).
6. Test harness: `tests/conftest.py` provides (a) the package-import bootstrap (import the
   hyphenated dir the way Hermes does, alias to `hermes_flaky_stabilization`), (b)
   `FakePluginContext` modeled on flaky-healer's (records tools/hooks/commands/skills; scripted
   `dispatch_tool`; `FakeLLM` with queued structured responses; `fire_hook` helper), (c)
   `profile_env` fixture (tmp `HERMES_HOME`, `Path.home` monkeypatch), (d) `HERMES_REPO`
   detection for integration tests (skip cleanly when absent — jira-incidents pattern).
7. `scripts/run_tests.sh`: CI-parity wrapper (pins `TZ=UTC`, `LANG/LC_ALL=C.UTF-8`,
   `PYTHONHASHSEED=0`, unsets `GITHUB_TOKEN`/`JIRA_API_TOKEN`/cloud creds, runs
   `python3 -m pytest "${@:-tests/}"`).

**Acceptance criteria (machine-checkable)**
- `bash scripts/run_tests.sh` → all green.
- `tests/test_register.py`: `register(FakeCtx)` completes; registry snapshot contains exactly the
  Phase-1 surface; a second `register()` call on a fresh ctx works (idempotent module state).
- Loader-parity subprocess test passes (imports the plugin from a foreign CWD with a cleared
  `PYTHONPATH`, exactly as Hermes does).
- `tests/test_storage_state.py`: fresh DB reaches `SCHEMA_VERSION`; re-open is idempotent; file
  modes are `0600`/dir `0700`.

### Phase 2 — Port `history` + `detective`

**Files:** `hermes_flaky_stabilization/history/*` (from `../hermes-test-history/`),
`hermes_flaky_stabilization/detective/*` (from `../hermes-flaky-detective/`), their tests under
`tests/history/`, `tests/detective/`, registration wiring for `test_failure_lookup`,
`module_failure_history`, `is_flaky`, and the CLI groups (`flaky-stab ingest|status|prune|
rebuild-fts|scan|list` + `test-history` alias).

**Tasks**
1. Copy modules verbatim; adapt imports; history keeps its own `history.db` storage module
   (path, PRAGMAs, schema untouched). Detective's `storage.py` is replaced by a thin adapter over
   `storage/state.py` (tables `flaky_verdicts`, `scan_runs`); its `query.py` (read-only reader of
   `history.db`) is kept but now *may* import `history.queries` helpers (same package — the
   GPL-boundary re-implementation is no longer needed; keep behavior identical either way).
2. Port both plugins' test suites; update conftest bootstraps to the unified package.
3. Contract-parity tests: for a seeded fixture `history.db`, assert the unified
   `test_failure_lookup` / `module_failure_history` / `is_flaky` handlers return **JSON deep-equal**
   to the outputs of the legacy handlers (import the legacy repos directly in the test via
   `sys.path` — they are available as siblings; mark `skipif` when absent so CI without siblings
   still runs the rest).
4. Schema-snapshot tests: the three registered tool schemas deep-equal frozen JSON snapshots
   checked into `tests/snapshots/`.

**Acceptance criteria**
- `bash scripts/run_tests.sh tests/history tests/detective` green (ported suites intact).
- Parity + snapshot tests green.
- `python3 -m pytest tests/test_register.py` shows the 3 tools registered with original toolsets.
- CLI: in a tmp `HERMES_HOME`, `flaky-stab ingest <fixture.xml>` then `flaky-stab scan --format
  json` produces a verdict for the seeded flaky fixture (assert in `tests/test_cli.py`).

### Phase 3 — Port `pii` + `bugreport` + `triage` (with the enrichment fix)

**Files:** `pii/detectors.py`, `pii/scanner.py` (from masking-validator), `pii/redaction.py`
(from jira-incidents), `bugreport/*`, `triage/*`, tests under `tests/pii/`, `tests/bugreport/`,
`tests/triage/`; registration wiring for `validate_no_pii`, `improve_bug_report`,
`/improve-bug`, `triage_pipeline_failure`.

**Tasks**
1. Verbatim ports (masking-validator's dual-import shim is removed — the unified package gives it
   a stable import root; keep everything else, including the OCR runtime probe and all bounds).
2. Triage: replace `enrichment.py`'s dispatch with a direct call into
   `history.queries.test_failure_lookup(conn, test_id=<signal line>, limit=5, config=...)`
   (FTS fallback handles free text). Keep the module's non-fatal semantics (any exception → None).
   Keep `redact-before-everything` ordering; add a regression test asserting the enrichment result
   is real lookup data, not an error envelope (the §2 row 6 bug).
3. Port all three test suites incl. masking-validator's security tests (no-writes stat snapshot,
   no subprocess/eval source scan — rewrite the source-scan test to cover the whole unified
   package except `healer/sandbox` + `healer/gitflow` + `detective/cli install-cron`, which
   legitimately use subprocess; pin the allowlist explicitly).
4. Schema-snapshot + parity tests as in Phase 2 (LLM faked with `FakeLLM`).

**Acceptance criteria**
- `bash scripts/run_tests.sh tests/pii tests/bugreport tests/triage` green.
- Enrichment regression test green (proves the arg-key bug is fixed).
- Taxonomy drift guards ported and green (schema enum, prose block, tool description all list the
  six categories).
- `validate_no_pii` snapshot equals legacy schema (with the detector enum injected).

### Phase 4 — Port `healer`

**Files:** `healer/*` (all subpackages), `skills/flaky-healer/SKILL.md`, tests under
`tests/healer/` (incl. fixtures: traces, gh API captures, toy-app), registration wiring for the 4
tools + `/heal` + the skill + the two audit hooks.

**Tasks**
1. Verbatim port. `recipes/store.py` becomes an adapter on `storage/state.py` (tables
   `healer_runs`, `recipes`, `audit`; keep its v1→v2 migration knowledge as part of the
   legacy-migration importer instead). `config.data_dir` now defaults to
   `<home>/flaky-stabilization/` (patches under `patches/`, mirror `learned-patterns.md`); all
   `FLAKY_HEALER_*` env overrides keep working.
2. Keep both sandbox backends, the digest-pinned image, gitflow guards, `check_fn` on
   `fetch_ci_logs`, and zip/SSRF/token hardening byte-for-byte.
3. Port the full offline suite (`-m "not docker and not live"`), the docker-marked and live-marked
   suites unchanged. The toy-app fixture (with committed node_modules) copies over as-is.
4. Snapshot/parity tests for the 4 tool schemas and the `/heal` command description.

**Acceptance criteria**
- `bash scripts/run_tests.sh tests/healer -- -m "not docker and not live"` green (~218 tests).
- `tests/test_register.py` updated: 8 tools now registered; hooks `pre_approval_request`,
  `post_approval_response` registered; skill `flaky-healer` registered with an existing path;
  `fetch_ci_logs` hidden when `GITHUB_TOKEN` unset and visible when set (check_fn behavior).
- Audit-hook test: firing the hooks through `FakePluginContext.fire_hook` writes redacted rows
  into `state.db:audit` (token-shaped values masked).

### Phase 5 — Port `incidents` (as general tools) + lifecycle hooks

**Files:** `incidents/{jira_client, ingest, store, prefetch, sync, cli}.py`,
`orchestrator/hooks.py` (partial: `pre_llm_call` + `on_session_start`), tests under
`tests/incidents/`; registration wiring for the 3 Jira read tools + `flaky-stab jira sync|status`.

**Tasks**
1. Port modules; `store.py` becomes an adapter on `state.db` (tables `incidents`, `incidents_fts`,
   `links`, `meta`; standalone FTS5 kept). Delete the `MemoryProvider` class; make sure the
   strings `register_memory_provider` / `MemoryProvider` appear **nowhere** in the package
   (kind auto-detection hazard, §3.2 — add a source-scan test).
2. Re-home the egress discipline: every tool handler redacts exactly as the provider did
   (`redact_incident` field lists preserved, `_json_egress` + strict canary honored). Port
   `test_no_pii_leak.py` and make it pass against the new tool handlers.
3. `pre_llm_call` hook: config-gated (`incidents.context_injection`), local-FTS-only,
   `prefetch_timeout`-bounded via the ported `PrefetchCache`, returns `{"context": <redacted>}` or
   `None`; never raises; never does network I/O on the turn path.
4. `on_session_start` hook: non-blocking `SyncScheduler.trigger()` when `JIRA_API_TOKEN` +
   base URL resolve; silent no-op otherwise. Tool reads also trigger a debounced sync.
5. check_fn for the three read tools: token+URL present **or** `incidents` table non-empty.

**Acceptance criteria**
- `bash scripts/run_tests.sh tests/incidents` green, including the ported no-PII-leak suite.
- Hook tests: `fire_hook("pre_llm_call", user_message=<seeded query>)` returns a `{"context":...}`
  dict whose text contains `[redacted-email]` for seeded PII and completes under the configured
  timeout with a deliberately slow store stub; returns `None` when the config flag is off.
- Source-scan test proves the forbidden memory-provider strings are absent from `__init__.py`.
- Sync test: `FakeTransport`-backed `flaky-stab jira sync` populates `incidents` +
  watermark in `meta`; second run with unchanged data performs zero FTS rewrites.

### Phase 6 — Orchestrator, approval escalation, PII gate, tracker write

**Files:** `orchestrator/{pipeline, dedup}.py`, `incidents/write.py`, completion of
`orchestrator/hooks.py` (`pre_tool_call`), registration wiring for `stabilize_test_failure`,
`find_duplicate_incidents`, `jira_create_incident`, `/stabilize`; tests under
`tests/orchestrator/`.

**Tasks**
1. `pipeline.py` implements §6.3 exactly; stage functions are injected (constructor takes the
   stage callables + config + connections) so tests can fake any stage.
2. Loop 1 (heal → history): `history.ingest` gains an internal helper
   `ingest_synthetic_run(conn, *, suite_name="flaky-healer-burnin", cases=[...])` writing normal
   `test_runs`/`test_cases` rows (schema unchanged — it is just another run; `source_file` set to
   a descriptive pseudo-path). Loop 2 (incidents → triage) via the enrichment seam from Phase 3.
3. `dedup.py`: tokenize summary; FTS OR-query over `incidents_fts`; rank by bm25 + token-overlap
   ratio; return `{candidates:[{key, summary, status, score}], count}` (redacted).
4. `incidents/write.py`: `create_incident(client, config, {title, body, labels?, severity?})` →
   POST `/rest/api/{v}/issue` with the config field mapping (`jira.project_key`,
   `jira.issue_type`, Appendix B); refuses when `jira.enable_write` false; every field passes
   `redact_text` first; returns `{created: true, key, url}` or an error envelope that never echoes
   the request body.
5. PII gate helper `orchestrator.gate.assert_evidence_clean(paths) -> GateResult` wrapping
   `pii.scanner.scan`; the pipeline and `jira_create_incident` both consume it
   (`clean && complete` required; failures return
   `{success:false, error:"pii_gate_failed", findings_summary, remediation}`).
6. `pre_tool_call` hook: returns `{"action":"approve", "message":..., "rule_key":
   "flaky_stab_pr"|"flaky_stab_tracker_write"}` for `heal_flaky_test` with `args.mode=="pr"` and
   for `jira_create_incident`; returns `None` for everything else (must not slow the hot path —
   pure dict inspection, no I/O).

**Acceptance criteria**
- Orchestrator tests (all with fakes, offline) cover: flaky fork → healer called, bug fork →
  bugreport called; stable heal → synthetic run visible via `test_failure_lookup`
  (`total_runs` increments); dirty evidence → pipeline stops with `pii_gate_failed` and **no**
  tracker call recorded; `enable_write=false` → outcome `ticket_ready` with redacted body;
  `enable_write=true` + `FakeTransport` → POST body contains `[redacted-email]` for seeded PII and
  outcome `ticket_created`; every stage_results key present in every outcome.
- Hook test: `fire_hook("pre_tool_call", tool_name="heal_flaky_test", args={"mode":"pr",...})`
  yields the approve directive; `mode="suggest"` yields None; `jira_create_incident` always yields
  the directive.
- Snapshot tests for the 3 new tool schemas.

### Phase 7 — CLI consolidation, legacy data migration, cron install, docs

**Files:** `cli.py` finalized, `storage/migrate_legacy.py`, `scripts/flaky-stab-scan.sh`,
`README.md`, `MIGRATION.md`, `after-install.md`, `docs/DECISIONS.md` updates; tests
`tests/test_cli.py`, `tests/test_migrate.py`, `tests/test_install_cron.py`.

**Tasks**
1. `flaky-stab migrate [--dry-run]`: for each legacy DB that exists —
   `<home>/flaky-detective/verdicts.db` (→ `flaky_verdicts`, `scan_runs`),
   `<home>/cache/ci_triage_patterns.db` (→ `triage_patterns` (+FTS rebuild)),
   `<legacy healer data_dir>/healer.db` (→ `healer_runs`, `recipes`, `audit`; apply the v1→v2
   relaxed-key backfill when importing v1 data),
   `<home>/hermes-jira-incidents.db` (→ `incidents` (+FTS), `links`, `meta`) —
   copy rows via `ATTACH` + `INSERT OR IGNORE ... SELECT`, record provenance rows in `meta`
   (`migrated_from_<name> = <path> @ <iso-ts> rows=<n>`), **never modify the sources**.
   Idempotent: re-running adds nothing (assert row counts stable).
2. `install-cron` per D10, with `subprocess.run` mocked in tests (legacy detective test pattern).
3. Docs: README (feature map, config reference, security model), MIGRATION.md (§10 content:
   disable-legacy list, migrate command, rollback, breaking changes — skill qualifier, healer
   data dir, memory-provider removal), `after-install.md` (rendered by `hermes plugins install`:
   enable command, token setup, migrate + install-cron hints).

**Acceptance criteria**
- `tests/test_migrate.py`: builds all four legacy fixture DBs, runs migrate twice, asserts (a)
  unified counts equal legacy counts, (b) second run is a no-op, (c) source files byte-identical
  before/after (hash), (d) `--dry-run` writes nothing.
- `tests/test_install_cron.py`: config persisted, shim installed `0700`, `hermes cron create`
  invoked with the configured schedule; graceful copy-paste fallback when the CLI is missing.
- `flaky-stab status` prints: DB paths, schema versions, per-table counts, config summary, cred
  presence (booleans only — never token values). Asserted in `tests/test_cli.py`.

### Phase 8 — Integration with real Hermes core, smoke test, final QA

**Files:** `tests/integration/test_discovery.py`, `tests/integration/test_registry.py`,
`scripts/smoke.sh`.

**Tasks**
1. Integration tests (skip cleanly unless `HERMES_REPO` or a known checkout path resolves —
   jira-incidents conftest pattern): copy the plugin into a tmp `HERMES_HOME/plugins/`, opt in via
   `plugins.enabled` in a generated `config.yaml`, run the real
   `hermes_cli.plugins.PluginManager().discover_and_load()`, then assert: plugin loaded with no
   error; all 16 tools present in `tools.registry` under their expected toolsets;
   5 hooks in `pm._hooks`; skill `hermes-flaky-stabilization:flaky-healer` resolvable;
   `flaky-stab` and `test-history` in `pm._cli_commands`; a broken submodule (temporarily injected)
   fails the load naming the real module.
2. `scripts/smoke.sh` (§9) — end-to-end on a disposable profile.
3. Final QA: full suite + coverage gate ≥ 85% on the unified package; `ruff check .` clean (config
   copied from healer's pyproject); run the Definition of Done checklist (§12) and write the
   results into `docs/DoD-REPORT.md`.

**Acceptance criteria**
- `HERMES_REPO=/home/sergi/hermes-agent bash scripts/run_tests.sh` → entire suite green including
  integration.
- `bash scripts/smoke.sh` exits 0 and prints the pipeline envelope of a seeded run.
- Coverage ≥ 85%; ruff clean; DoD report committed with every box checked.

---

## 8. Testing strategy

Four layers (all runnable offline by default; network/docker strictly opt-in):

1. **Ported unit suites** — each legacy plugin's tests move with its code (~4,500 test-LOC
   combined; jira-incidents alone has 171 test functions). They are the behavioral spec; a ported
   test may only be edited for import paths and storage fixtures, never for expectations (any
   necessary expectation change is a plan deviation to document).
2. **Contract tests** — frozen JSON schema snapshots for all 16 tools; JSON-deep-equality parity
   tests against the legacy handlers (siblings on `sys.path`, `skipif` when absent); error-envelope
   shape tests (`{success:false, error, remediation}` / `{"error":...}` families preserved
   per stage).
3. **Orchestrator/integration-with-fakes** — `FakePluginContext` + `FakeLLM` + `FakeTransport` +
   `FakeSandbox` (all ported from healer/jira test doubles); full-pipeline scenarios per Phase 6.
4. **Integration with the real core** — gated on a hermes-agent checkout (`HERMES_REPO`), using
   the real `PluginManager` loader and `tools.registry` (Phase 8); plus opt-in `-m docker` (real
   sandbox heals) and `-m live` (real GitHub/Jira, needs tokens) markers preserved from the
   legacy suites.

Conventions: `scripts/run_tests.sh` is the only sanctioned entry (CI parity: `TZ=UTC`,
`C.UTF-8`, `PYTHONHASHSEED=0`, credential env scrubbed); `pytest` config in `pyproject.toml`
(`testpaths=["tests"]`, `--import-mode=importlib`, default `-m "not docker and not live"`);
fixtures under `tests/fixtures/` (JUnit XMLs, traces, GH captures, toy-app, seeded legacy DBs).

---

## 9. Install, load and smoke-test inside Hermes

Manual procedure (mirrored by `scripts/smoke.sh` against a disposable `HERMES_HOME`):

```bash
# 1. Install (either)
hermes plugins install sergiparpal/hermes-flaky-stabilization
#    or: ln -s <this repo> ~/.hermes/plugins/hermes-flaky-stabilization

# 2. Enable (standalone plugins are opt-in)
hermes plugins enable hermes-flaky-stabilization
hermes plugins disable hermes-test-history hermes-flaky-detective hermes-ci-triage \
  hermes-flaky-healer hermes-bug-report-improver hermes-masking-validator   # avoid tool collisions
# If hermes-jira-incidents was the active memory provider: clear memory.provider in config.yaml.

# 3. Verify discovery
HERMES_PLUGINS_DEBUG=1 hermes plugins list        # plugin shows enabled, no error
hermes tools                                       # toolsets 🔌 test_history, flaky_detective,
                                                   # ci_triage, flaky_healer, qa, qa_masking,
                                                   # jira_incidents, flaky_stabilization

# 4. Migrate + exercise
hermes flaky-stab migrate
hermes flaky-stab status
hermes test-history ingest tests/fixtures/pytest_junit_failures.xml
hermes flaky-stab scan --format json
hermes -z "triage this CI log: tests/fixtures/sample-fail.log"   # tool-call round trip
hermes flaky-stab install-cron --schedule "0 9 * * *"
```

Smoke pass = every command exits 0, the triage answer names one of the six categories, and
`flaky-stab status` shows migrated counts.

---

## 10. Migration and rollback

**User migration (MIGRATION.md content):**
1. Install + enable the unified plugin; disable the six standalone legacy plugins (tool-name
   collisions are otherwise rejected by the registry); if `memory.provider` pointed at
   `hermes-jira-incidents`, unset it (the slot is now free for any other provider).
2. Run `hermes flaky-stab migrate` — copies verdicts, triage patterns, healer runs/recipes/audit,
   and the incidents index into `state.db`. `history.db` needs no migration (unchanged path and
   schema). Legacy DBs are left untouched.
3. Re-point any crontabs: `hermes cron` job `flaky-detective` → run
   `hermes flaky-stab install-cron` and delete the old job.
4. Env vars unchanged (`GITHUB_TOKEN`, `JIRA_API_TOKEN`, `FLAKY_HEALER_*` still honored).

**Breaking changes (documented, accepted):** skill qualifier becomes
`hermes-flaky-stabilization:flaky-healer`; healer data dir default moves under
`flaky-stabilization/` (env override preserves the old location); jira-incidents' automatic
system-prompt block is gone (replaced by `pre_llm_call` injection); its `hermes-jira-incidents`
CLI attribute is replaced by `flaky-stab jira ...`.

**Rollback:** disable the unified plugin, re-enable the legacy seven (and re-set
`memory.provider` if desired). Because migration copies rather than moves, all legacy state is
exactly where it was; data written to `state.db` after migration does not flow back (accepted and
documented — rollback loses only post-migration verdicts/patterns/recipes/incidents deltas;
`history.db` loses nothing since both worlds share it).

---

## 11. Strategy for the seven existing repos

Per D8: **deprecate + absorb.** For each of the seven repos, after the unified plugin's first
tagged release: (1) cut a final legacy release; (2) add a README banner — "superseded by
`hermes-flaky-stabilization`, see MIGRATION.md" with a link; (3) archive the GitHub repo
(read-only). No dual-maintenance window: bug fixes land only in the unified repo. The unified
repo's `git log` records the source commit of each absorbed plugin
(`Absorbed-From: sergiparpal/<repo>@<sha>` trailers in the porting commits) to preserve
provenance. This step touches external repos and is the **only** part of the plan the
implementing agent must leave as a prepared-but-not-executed script
(`scripts/deprecate-legacy.sh` emitting the `gh` commands) unless the user explicitly asks to run
it.

---

## 12. Definition of Done

All must hold, verified by the implementing agent and recorded in `docs/DoD-REPORT.md`:

- [ ] `plugin.yaml` (Appendix B) + root shim + `register(ctx)` load under the real
      `PluginManager` with zero errors (Phase 8 integration test).
- [ ] All 13 legacy tools registered with byte-identical schemas (snapshot tests) and
      JSON-parity behavior (parity tests); the 3 new tools registered and snapshot-frozen.
- [ ] `history.db` path, schema (version 1) and CLI alias unchanged — keystone contract intact.
- [ ] All ported test suites green offline: `bash scripts/run_tests.sh` (no docker, no network,
      no credentials); coverage ≥ 85%; `ruff check .` clean.
- [ ] `HERMES_REPO=... run_tests.sh` green including real-loader integration tests.
- [ ] No-PII-leak suite green against the new tool handlers; PII gate blocks a seeded-dirty
      pipeline run; `jira_create_incident` redacts outbound fields and is hidden/off by default.
- [ ] Approval escalation directives verified for `heal_flaky_test(mode=pr)` and
      `jira_create_incident`; healer PR flow still refuses unstable/empty/subprocess-isolated
      heals; git/PR steps go exclusively through `ctx.dispatch_tool`.
- [ ] `pre_llm_call` incident injection: bounded, redacted, config-gated, offline.
- [ ] `flaky-stab migrate` idempotent, sources untouched (hash-verified), provenance recorded.
- [ ] `install-cron` installs the no-agent job (or prints the fallback command).
- [ ] `scripts/smoke.sh` passes on a disposable profile.
- [ ] README, MIGRATION.md, after-install.md, DECISIONS.md, DoD-REPORT.md committed.
- [ ] The strings `register_memory_provider`/`MemoryProvider` absent from `__init__.py`
      (kind auto-detection guard test).
- [ ] Zero modifications to Hermes core or to the seven legacy repos.

---

## Appendix A — unified `state.db` DDL

`<hermes_home>/flaky-stabilization/state.db`. WAL; dir `0700`, file `0600`;
`SCHEMA_VERSION = 1` (single migration step; future changes append ladder steps). Legacy column
sets are preserved exactly to make migration a plain `INSERT ... SELECT`.

```sql
-- detective (from verdicts.db)
CREATE TABLE IF NOT EXISTS flaky_verdicts(
  test_key TEXT PRIMARY KEY, classname TEXT, name TEXT NOT NULL, file_path TEXT,
  passes INTEGER NOT NULL, fails INTEGER NOT NULL, runs INTEGER NOT NULL,
  window_days INTEGER NOT NULL, first_seen TIMESTAMP, last_seen TIMESTAMP,
  last_failure TIMESTAMP, status TEXT NOT NULL,
  computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_verdicts_status ON flaky_verdicts(status);
CREATE INDEX IF NOT EXISTS idx_verdicts_name   ON flaky_verdicts(name);
CREATE TABLE IF NOT EXISTS scan_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ran_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  window_days INTEGER NOT NULL, min_fails INTEGER NOT NULL, include_errors INTEGER NOT NULL,
  source_schema_version INTEGER, tests_examined INTEGER NOT NULL, flaky_found INTEGER NOT NULL);

-- triage (from ci_triage_patterns.db; renamed patterns -> triage_patterns)
CREATE TABLE IF NOT EXISTS triage_patterns(
  project TEXT NOT NULL, signature TEXT NOT NULL, category TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  sample TEXT NOT NULL DEFAULT '', PRIMARY KEY (project, signature));
CREATE INDEX IF NOT EXISTS idx_triage_patterns_last_seen ON triage_patterns(project, last_seen);
CREATE VIRTUAL TABLE IF NOT EXISTS triage_patterns_fts USING fts5(
  sample, project UNINDEXED, signature UNINDEXED);          -- when FTS5 available; LIKE fallback

-- healer (from healer.db; runs renamed healer_runs; v2 relaxed_key included from the start)
CREATE TABLE IF NOT EXISTS healer_runs(
  id INTEGER PRIMARY KEY, ts TEXT, source TEXT, build_id TEXT, test_id TEXT,
  diagnosis_json TEXT, strategy TEXT, result TEXT, isolation TEXT, duration_s REAL);
CREATE TABLE IF NOT EXISTS recipes(
  signature TEXT PRIMARY KEY, created_ts TEXT, diagnosis_json TEXT, strategy TEXT,
  patch_ops_json TEXT, hits INTEGER DEFAULT 0, successes INTEGER DEFAULT 0,
  failures INTEGER DEFAULT 0, last_used_ts TEXT, relaxed_key TEXT);
CREATE INDEX IF NOT EXISTS idx_recipes_relaxed_key ON recipes(relaxed_key);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts TEXT, event TEXT, payload_json TEXT);

-- incidents (from hermes-jira-incidents.db)
CREATE TABLE IF NOT EXISTS incidents(
  key TEXT PRIMARY KEY, summary TEXT, status TEXT, root_cause TEXT, reporter TEXT,
  assignee TEXT, created TEXT, updated TEXT, body TEXT, raw_json TEXT, indexed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_incidents_updated ON incidents(updated);
CREATE VIRTUAL TABLE IF NOT EXISTS incidents_fts USING fts5(
  key, summary, status, root_cause, body, tokenize='unicode61'); -- standalone (TEXT PK)
CREATE TABLE IF NOT EXISTS links(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, incident_key TEXT,
  note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- orchestrator (new)
CREATE TABLE IF NOT EXISTS pipeline_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, trigger TEXT NOT NULL,
  test_id TEXT, project TEXT, triage_category TEXT, branch TEXT,   -- 'heal' | 'bug' | NULL
  outcome TEXT NOT NULL, stage_results_json TEXT NOT NULL, duration_s REAL);

CREATE TABLE IF NOT EXISTS schema_version(
  version INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
```

`history.db` keeps its own file and schema (§4.1) — not part of this DDL by design (D4).

## Appendix B — consolidated config file spec

**`plugin.yaml`** (manifest):

```yaml
name: hermes-flaky-stabilization
version: 0.1.0
description: "Unified flaky-test stabilization pipeline: failure history, flaky detection,
  CI triage, sandboxed healing with PR flow, bug-report structuring, PII gating, and a local
  Jira incident index — one plugin, one state store."
author: "sergiparpal"
kind: standalone
manifest_version: 1
# requires_env intentionally omitted: GITHUB_TOKEN and JIRA_API_TOKEN are optional and gate
# individual tools via check_fn; a missing token must never disable the whole plugin.
provides_tools:
  - test_failure_lookup
  - module_failure_history
  - is_flaky
  - triage_pipeline_failure
  - fetch_ci_logs
  - analyze_playwright_trace
  - heal_flaky_test
  - list_healing_recipes
  - improve_bug_report
  - validate_no_pii
  - jira_search_incident
  - jira_get_root_cause
  - jira_link_session
  - jira_create_incident
  - find_duplicate_incidents
  - stabilize_test_failure
provides_hooks:
  - pre_tool_call
  - pre_llm_call
  - on_session_start
  - pre_approval_request
  - post_approval_response
```

**`<hermes_home>/flaky-stabilization/config.json`** (all keys optional; defaults shown; malformed
file degrades to defaults; env overrides in comments):

```jsonc
{
  "history":   { "default_lookback_days": 30, "max_stack_trace_chars": 500,
                 "db_path_override": null },                  // constrained inside hermes home
  "detective": { "window_days": 14, "min_fails": 3, "include_errors": true,
                 "schedule": "0 9 * * *", "deliver": "local",
                 "report_scope": "changes-only" },
  "triage":    { "enable_enrichment": true, "log_roots": null,   // env HERMES_CI_TRIAGE_LOG_ROOTS
                 "token_hosts": null, "allow_private": false },  // env HERMES_CI_TRIAGE_* win
  "healer":    { "burnin": "5:10", "sandbox": "auto", "docker_image": null,
                 "git_tool": "terminal", "pr_tool": "create_pull_request",
                 "base_branch": "main", "allow_subprocess_pr": false },
                 // every FLAKY_HEALER_* env var still overrides its key
  "pii":       { "default_max_files": 2000 },
  "jira":      { "base_url": "", "email": "", "auth_mode": "api_token",
                 "jql": "project = INC ORDER BY updated DESC", "root_cause_field": null,
                 "page_size": 50, "max_pages": 20, "retention_days": 0,
                 "sync_min_interval": 60.0,
                 "enable_write": false, "project_key": "INC", "issue_type": "Bug" },
                 // secret only via env JIRA_API_TOKEN; JIRA_EMAIL/JIRA_BASE_URL overrides honored
  "incidents": { "context_injection": true, "prefetch_limit": 3, "prefetch_timeout": 1.5 },
  "pipeline":  { "default_heal_mode": "suggest",        // "suggest" | "pr"
                 "heal_categories": ["flaky", "timeout"],
                 "require_pii_gate": true }              // never set false in production docs
}
```

## Appendix C — legacy → unified mapping tables

**DBs:** `flaky-detective/verdicts.db` → `state.db` (`flaky_verdicts`, `scan_runs`) ·
`cache/ci_triage_patterns.db` → `state.db` (`triage_patterns` + FTS) ·
`plugins-data/hermes-flaky-healer/healer.db` → `state.db` (`healer_runs`, `recipes`, `audit`) ·
`hermes-jira-incidents.db` → `state.db` (`incidents` + FTS, `links`, `meta`) ·
`test-history/history.db` → **unchanged**.

**Config files:** `test-history/config.json` → `config.json:history` ·
`flaky-detective/config.json` → `config.json:detective` ·
`hermes-jira-incidents.json` → `config.json:jira` · (ci-triage and healer had env-only config →
`config.json:triage`/`healer` with env still winning) · masking-validator had none.

**CLI:** `hermes test-history ...` → kept as alias (also `flaky-stab ingest|status|prune|
rebuild-fts`) · `hermes flaky-detective scan|status|list|install-cron` →
`hermes flaky-stab scan|status|list|install-cron` · jira-incidents CLI → `hermes flaky-stab jira
sync|status`.

**Cron:** job `flaky-detective` + `flaky-scan.sh` → job `flaky-stabilization` +
`flaky-stab-scan.sh`.

**Hooks:** memory-provider `prefetch()` → `pre_llm_call` injection · `sync_turn`/`initialize`
sync triggers → `on_session_start` + pre-read debounced triggers · healer approval observers →
unchanged · (new) `pre_tool_call` approval escalation.
