#!/usr/bin/env bash
#
# Deprecate + archive the seven legacy repos (plan §11, D8).
#
# PREPARED BUT NOT EXECUTED by the implementing agent: this touches external
# GitHub repos, so a human runs it deliberately, after the unified plugin's
# first tagged release. It only PRINTS the gh/git commands by default; pass
# --execute to run them.
#
# Per repo: (1) add a README deprecation banner + final commit, (2) tag a final
# release, (3) archive the repo (read-only).
set -euo pipefail

REPOS=(
  hermes-test-history
  hermes-flaky-detective
  hermes-ci-triage
  hermes-flaky-healer
  hermes-bug-report-improver
  hermes-masking-validator
  hermes-jira-incidents
)
OWNER="sergiparpal"
UNIFIED="https://github.com/${OWNER}/hermes-flaky-stabilization"
BANNER="> **DEPRECATED — superseded by [hermes-flaky-stabilization](${UNIFIED}).**\\n> Bug fixes land only there; see its MIGRATION.md. This repo is archived read-only."

MODE="print"
[ "${1:-}" = "--execute" ] && MODE="execute"

run() {
  if [ "$MODE" = "execute" ]; then
    echo "+ $*"
    "$@"
  else
    echo "  $*"
  fi
}

for repo in "${REPOS[@]}"; do
  echo "# --- ${repo} -------------------------------------------------"
  dir="../${repo}"
  echo "# 1) banner + final commit"
  run bash -c "printf '%b\n\n' \"${BANNER}\" | cat - '${dir}/README.md' > '${dir}/README.md.new' && mv '${dir}/README.md.new' '${dir}/README.md'"
  run git -C "${dir}" add README.md
  run git -C "${dir}" commit -m "Deprecate: superseded by ${OWNER}/hermes-flaky-stabilization"
  run git -C "${dir}" push
  echo "# 2) final release"
  run gh release create v-final --repo "${OWNER}/${repo}" --title "Final release (deprecated)" \
      --notes "Superseded by ${UNIFIED}. See its MIGRATION.md."
  echo "# 3) archive (read-only)"
  run gh repo archive "${OWNER}/${repo}" --yes
  echo
done

if [ "$MODE" = "print" ]; then
  echo "Dry run only. Re-run with --execute to perform the above."
fi
