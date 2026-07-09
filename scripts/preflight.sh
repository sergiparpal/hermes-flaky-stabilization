#!/usr/bin/env bash
# Phase 0 preflight: verify everything the implementation needs is present.
# Exits non-zero naming the first missing item.
set -u

fail() { echo "PREFLIGHT FAIL: $1" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$ROOT")"

for repo in hermes-test-history hermes-flaky-detective hermes-ci-triage \
            hermes-flaky-healer hermes-bug-report-improver hermes-masking-validator \
            hermes-jira-incidents; do
  [ -d "$PARENT/$repo" ] || fail "sibling repo missing: $PARENT/$repo"
done

HERMES_REPO="${HERMES_REPO:-$PARENT/hermes-agent}"
[ -d "$HERMES_REPO" ] || fail "hermes-agent checkout missing: $HERMES_REPO"
[ -f "$HERMES_REPO/hermes_cli/plugins.py" ] || fail "hermes-agent checkout incomplete: no hermes_cli/plugins.py"

command -v python3 >/dev/null 2>&1 || fail "python3 not on PATH"
python3 - <<'EOF' || exit 1
import sys
if sys.version_info < (3, 11):
    print(f"PREFLIGHT FAIL: python >= 3.11 required, found {sys.version}", file=sys.stderr)
    raise SystemExit(1)
try:
    import pytest  # noqa: F401
except ImportError:
    print("PREFLIGHT FAIL: pytest not importable", file=sys.stderr)
    raise SystemExit(1)
import sqlite3
conn = sqlite3.connect(":memory:")
try:
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
except sqlite3.OperationalError:
    print("PREFLIGHT WARN: sqlite FTS5 unavailable (LIKE fallbacks will be used)", file=sys.stderr)
finally:
    conn.close()
EOF
[ $? -eq 0 ] || exit 1

echo "PREFLIGHT OK: 7 sibling repos, hermes-agent at $HERMES_REPO, $(python3 --version), pytest present"
