#!/usr/bin/env bash
#
# CI-parity test wrapper for hermes-flaky-stabilization (the only sanctioned
# test entry point, plan §8). Same environment hardening as the legacy plugin
# repos: credential env vars scrubbed so tests can never reach a real backend,
# deterministic timezone/locale/hash-seed.
#
# Usage:
#   scripts/run_tests.sh                       # whole suite (tests/)
#   scripts/run_tests.sh tests/history         # one stage
#   scripts/run_tests.sh tests/healer -- -m "not docker and not live"
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Credential-bearing env vars: unset so no test can accidentally go live.
unset -v \
  GITHUB_TOKEN JIRA_API_TOKEN JIRA_EMAIL JIRA_BASE_URL \
  OPENAI_API_KEY ANTHROPIC_API_KEY HERMES_API_KEY GEMINI_API_KEY GOOGLE_API_KEY \
  GROQ_API_KEY MISTRAL_API_KEY OPENROUTER_API_KEY XAI_API_KEY DEEPSEEK_API_KEY \
  TOGETHER_API_KEY FIREWORKS_API_KEY COHERE_API_KEY HF_TOKEN \
  AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AZURE_OPENAI_API_KEY 2>/dev/null || true

# Plugin-specific env that would leak host state into tests.
for var in $(compgen -e | grep -E '^(FLAKY_HEALER_|HERMES_CI_TRIAGE_|HERMES_JIRA_)' || true); do
  unset -v "$var"
done

export TZ=UTC
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONHASHSEED=0

# Allow `scripts/run_tests.sh tests/foo -- -k pattern` style pass-through.
args=()
for a in "$@"; do [ "$a" = "--" ] || args+=("$a"); done

exec python3 -m pytest "${args[@]:-tests/}"
