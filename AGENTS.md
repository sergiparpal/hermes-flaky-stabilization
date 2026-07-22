# Repository Guidelines

## Project Structure & Module Organization

Runtime code lives in `hermes_flaky_stabilization/`. Feature stages are separated into
`history/`, `detective/`, `triage/`, `healer/`, `bugreport/`, `pii/`, and `incidents/`;
`orchestrator/` composes them, `storage/` owns SQLite infrastructure, and `common/` is the
dependency-light kernel. Keep Hermes-facing wiring in `registration.py` and `cli.py`.

Tests mirror the package under `tests/<stage>/`. Cross-stage and integration checks live directly
under `tests/` and `tests/integration/`; stable tool contracts are captured in `tests/snapshots/`.
Operational scripts are in `scripts/`, plugin metadata is in `plugin.yaml`, and design records are
in `docs/`.

## Build, Test, and Development Commands

Use Python 3.11–3.13.

```bash
python3 -m pip install -e ".[dev]"       # editable install with pytest and Ruff
bash scripts/run_tests.sh                # sanctioned offline test suite
bash scripts/run_tests.sh tests/history  # run one stage
bash scripts/run_tests.sh -- -k migrate  # run a focused selection
ruff check .                             # lint and import-order checks
python3 -m pip wheel . --no-deps          # build a wheel
```

Set `HERMES_REPO=/path/to/hermes-agent` when running loader integration against a real checkout.

## Coding Style & Naming Conventions

Use four-space indentation, type hints for public interfaces, `snake_case` for modules/functions,
and `PascalCase` for classes. Ruff enforces `E`, `F`, `W`, `I`, `UP`, and `B` rules with a
100-character line limit. Preserve the targeted per-file exceptions in `pyproject.toml`; do not
mechanically restyle ported legacy modules. Maintain the enforced dependency direction:
`common` ← stages/storage ← `orchestrator` ← registration/CLI.

## Testing Guidelines

Write pytest files as `test_*.py` and name tests after observable behavior. Add tests beside the
affected stage and update snapshots only for intentional schema changes. Docker, end-to-end, and
live tests must use their declared markers; the default suite excludes all three and scrubs
credentials. Maintain at least 85% coverage:

```bash
bash scripts/run_tests.sh -- --cov=hermes_flaky_stabilization --cov-fail-under=85
```

## Commit & Pull Request Guidelines

Recent commits use short imperative subjects such as `Add LICENSE file` or scoped forms such as
`docs: update license to MIT`. Keep each commit focused. PRs should explain what changed, why,
user/developer impact, linked issues, and validation performed. Include screenshots only for
visible output changes and call out schema, migration, credential, PII, or external-write effects.

## Security & Configuration

Never commit tokens, local databases, or incident/test evidence containing PII. Keep
`GITHUB_TOKEN` and `JIRA_API_TOKEN` in the environment. Preserve fail-closed PII gates, bounded
network clients, and approval checks on PR/Jira write paths.
