#!/usr/bin/env bash
#
# Optional no-agent cron shim for hermes-flaky-stabilization (plan D10,
# `install-cron --with-jira-sync`): refresh the local Jira incident index on a
# schedule at zero LLM cost. `--quiet` (D10) suppresses the success chatter so
# a healthy tick delivers nothing; failures still print and exit non-zero.
# Exit codes propagate directly — `exec` replaces this shell with the sync
# process — so a broken sync alerts instead of failing silently, and
# `set -euo pipefail` guards any future lines added before the exec.
set -euo pipefail
exec hermes flaky-stab jira sync --quiet
