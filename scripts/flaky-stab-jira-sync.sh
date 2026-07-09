#!/usr/bin/env bash
#
# Optional no-agent cron shim for hermes-flaky-stabilization (plan D10,
# `install-cron --with-jira-sync`): refresh the local Jira incident index on a
# schedule at zero LLM cost. Exit codes propagate (exec), so a broken sync
# alerts instead of failing silently.
set -euo pipefail
exec hermes flaky-stab jira sync
