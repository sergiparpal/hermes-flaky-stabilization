#!/usr/bin/env bash
#
# End-to-end smoke test on a disposable profile (plan §9, Phase 8).
#
# Loads the plugin through the REAL hermes PluginManager in a throwaway
# HERMES_HOME, then exercises the pipeline: migrate legacy data, ingest JUnit
# results, run a detection scan, run stabilize_test_failure on a seeded CI log
# (heuristic triage — no LLM credentials needed), and stage the cron install.
# Prints the pipeline envelope; exits non-zero on any failure.
#
# Requirements: python3 and a hermes-agent checkout (HERMES_REPO env, default
# ../hermes-agent). With a fully-installed `hermes` CLI you can instead run
# the manual procedure in README.md/§9 verbatim.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_REPO="${HERMES_REPO:-$(dirname "$ROOT")/hermes-agent}"
[ -f "$HERMES_REPO/hermes_cli/plugins.py" ] || {
  echo "SMOKE FAIL: hermes-agent checkout not found at $HERMES_REPO (set HERMES_REPO)" >&2
  exit 1
}

export HERMES_HOME="$(mktemp -d -t flaky-stab-smoke.XXXXXX)"
trap 'rm -rf "$HERMES_HOME"' EXIT
echo "smoke profile: $HERMES_HOME"

# Install the plugin into the disposable profile and opt it in.
PLUG="$HERMES_HOME/plugins/hermes-flaky-stabilization"
mkdir -p "$PLUG"
cp "$ROOT/plugin.yaml" "$ROOT/__init__.py" "$PLUG/"
cp -r "$ROOT/hermes_flaky_stabilization" "$ROOT/skills" "$PLUG/"
mkdir -p "$PLUG/scripts" && cp "$ROOT"/scripts/*.sh "$PLUG/scripts/"
find "$PLUG" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
printf 'plugins:\n  enabled:\n    - hermes-flaky-stabilization\n' > "$HERMES_HOME/config.yaml"

# Seed one legacy DB so `migrate` has something to copy.
python3 - "$HERMES_HOME" <<'EOF'
import sqlite3, sys
from pathlib import Path
home = Path(sys.argv[1])
(home / "flaky-detective").mkdir(parents=True)
conn = sqlite3.connect(home / "flaky-detective" / "verdicts.db")
conn.executescript("""
  CREATE TABLE flaky_verdicts (test_key TEXT PRIMARY KEY, classname TEXT,
    name TEXT NOT NULL, file_path TEXT, passes INTEGER NOT NULL,
    fails INTEGER NOT NULL, runs INTEGER NOT NULL, window_days INTEGER NOT NULL,
    first_seen TIMESTAMP, last_seen TIMESTAMP, last_failure TIMESTAMP,
    status TEXT NOT NULL, computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
  CREATE TABLE scan_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, window_days INTEGER NOT NULL,
    min_fails INTEGER NOT NULL, include_errors INTEGER NOT NULL,
    source_schema_version INTEGER, tests_examined INTEGER NOT NULL,
    flaky_found INTEGER NOT NULL);
""")
conn.execute("INSERT INTO flaky_verdicts (test_key, name, passes, fails, runs,"
             " window_days, status) VALUES ('legacy::t', 't', 1, 3, 4, 14, 'flaky')")
conn.commit(); conn.close()
print("seeded legacy verdicts.db (1 verdict)")
EOF

# The real-loader smoke driver.
python3 - "$HERMES_REPO" "$HERMES_HOME" <<'EOF'
import argparse, json, sqlite3, sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

hermes_repo, home = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, hermes_repo)

# 1) Real discovery.
from hermes_cli.plugins import PluginManager
from tools.registry import registry

pm = PluginManager()
pm.discover_and_load()
loaded = pm._plugins["hermes-flaky-stabilization"]
assert loaded.enabled, f"plugin failed to load: {loaded.error!r}"
print(f"[1] plugin loaded: {len(loaded.tools_registered)} tools, "
      f"hooks={sorted(pm._hooks)}")

# 2) The CLI, exactly as `hermes flaky-stab …` would dispatch it.
entry = pm._cli_commands["flaky-stab"]
parser = argparse.ArgumentParser(prog="flaky-stab")
entry["setup_fn"](parser)
run = entry["handler_fn"]

def cli(*argv):
    rc = run(parser.parse_args(list(argv)))
    assert rc in (0, None), f"flaky-stab {' '.join(argv)} exited {rc}"

print("[2] flaky-stab migrate")
cli("migrate")

# 3) Ingest four freshly-stamped JUnit runs (3 fails + 1 pass = flaky).
now = datetime.now(UTC).replace(tzinfo=None)
for i, (days, status) in enumerate([(6, "failed"), (4, "failed"),
                                    (3, "passed"), (2, "failed")]):
    stamp = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    body = ('<failure message="boom" type="AssertionError">t</failure>'
            if status == "failed" else "")
    xml = home / f"run{i}.xml"
    xml.write_text(
        f'<?xml version="1.0"?><testsuite name="smoke" timestamp="{stamp}" '
        f'tests="1" failures="{1 if status == "failed" else 0}" errors="0" '
        f'skipped="0"><testcase classname="shop.cart" name="kw_checkout" '
        f'file="src/shop/cart.spec.ts" time="0.1">{body}</testcase></testsuite>',
        encoding="utf-8")
    cli("ingest", str(xml))
print("[3] ingested 4 JUnit runs")

# 4) Detection scan must classify the seeded test as flaky.
import contextlib, io
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cli("scan", "--format", "json")
verdicts = {v["test_key"]: v["status"] for v in json.loads(buf.getvalue())["verdicts"]}
assert verdicts.get("shop.cart::kw_checkout") == "flaky", verdicts
print("[4] scan verdict: shop.cart::kw_checkout is flaky")

# 5) The pipeline on a seeded CI log (heuristic path, no LLM credentials).
log = home / "ci-fail.log"
log.write_text("E   ModuleNotFoundError: No module named 'requests'\n"
               "FAILED tests/test_api.py::test_fetch\n", encoding="utf-8")
handler = registry.get_entry("stabilize_test_failure").handler
envelope = json.loads(handler({"log_url_or_path": str(log), "project": "smoke"}))
assert envelope["success"] is True
category = envelope["stage_results"]["triage"]["category"]
from hermes_flaky_stabilization.triage import taxonomy
assert category in taxonomy.TAXONOMY, category
print(f"[5] pipeline envelope (outcome={envelope['outcome']}, "
      f"triage category={category}):")
print(json.dumps(envelope, indent=2)[:2000])

# 6) status shows the migrated verdict counts.
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cli("status")
status = buf.getvalue()
assert "flaky_verdicts=" in status and "state.db" in status
print("[6] status OK (migrated counts visible)")

# 7) cron staging (no job creation without a gateway).
cli("install-cron", "--no-create", "--deliver", "local")
assert (home / "scripts" / "flaky-stab-scan.sh").exists()
print("[7] cron shim staged")

print("SMOKE OK")
EOF
echo "smoke passed."
