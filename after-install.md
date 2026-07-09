# hermes-flaky-stabilization — first steps

1. **Enable it** (standalone plugins are opt-in):

   ```bash
   hermes plugins enable hermes-flaky-stabilization
   ```

   Migrating from the seven legacy plugins? Disable them first and run
   `hermes flaky-stab migrate` — see MIGRATION.md.

2. **Optional credentials** (each unlocks one stage; nothing breaks without them):

   * `GITHUB_TOKEN` — unlocks `fetch_ci_logs` (CI-log fetching from GitHub Actions).
   * `JIRA_API_TOKEN` (+ `jira.base_url` in the config, or `JIRA_BASE_URL`) —
     unlocks the Jira incident sync and, only if you also set
     `jira.enable_write: true`, the `jira_create_incident` write-back.

3. **Feed it data:**

   ```bash
   hermes test-history ingest path/to/junit-reports/
   hermes flaky-stab scan            # detect flaky tests now
   hermes flaky-stab install-cron    # …or nightly, at zero LLM cost
   ```

4. **Use it:** ask the agent to `stabilize` a failing CI run, or call
   `/stabilize <ci-log-path>` directly. `hermes flaky-stab status` shows
   what the plugin knows.
