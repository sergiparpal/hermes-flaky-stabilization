#!/usr/bin/env python3
"""Capture real failing Playwright trace.zip fixtures from the toy app.

Runs each flaky spec repeatedly (max 10 attempts) until it fails, then copies
the failing test's trace.zip into fixtures/traces/<key>.trace.zip. Also saves
the raw `list`-reporter output of one failing flaky-selector run, which the
GitHub-logs fixture generator embeds into a realistic run_logs.zip.

Usage: python3 fixtures/scripts/collect_traces.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
FIXTURES = SCRIPTS_DIR.parent
TOY_APP = FIXTURES / "toy-app"
TRACES = FIXTURES / "traces"
GH = FIXTURES / "gh"

SPECS = {
    "selector": "tests/flaky-selector.spec.ts",
    "timeout": "tests/flaky-timeout.spec.ts",
    "race": "tests/flaky-race.spec.ts",
}
MAX_ATTEMPTS = 10


def run_spec(
    spec: str, reporter: str, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(extra_env or {})
    return subprocess.run(
        ["npx", "playwright", "test", spec, f"--reporter={reporter}"],
        cwd=TOY_APP,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def failing_trace_path(report: dict) -> Path | None:
    """Walk the JSON report; return the trace attachment of the first failed test."""

    def walk(suite):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                for result in test.get("results", []):
                    if result.get("status") not in ("passed", "skipped"):
                        for att in result.get("attachments", []):
                            if att.get("name") == "trace" and att.get("path"):
                                return Path(att["path"])
        for child in suite.get("suites", []):
            found = walk(child)
            if found:
                return found
        return None

    for suite in report.get("suites", []):
        found = walk(suite)
        if found:
            return found
    return None


def collect_trace(key: str, spec: str) -> int:
    """Return the number of attempts needed (0 = never failed)."""
    report_file = TOY_APP / "run-report.json"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        proc = run_spec(spec, "json", {"PLAYWRIGHT_JSON_OUTPUT_NAME": "run-report.json"})
        if proc.returncode == 0:
            print(f"  [{key}] attempt {attempt}: passed, retrying")
            continue
        report = json.loads(report_file.read_text())
        trace = failing_trace_path(report)
        if trace is None or not trace.is_file():
            print(f"  [{key}] attempt {attempt}: failed but no trace attachment found")
            continue
        dest = TRACES / f"{key}.trace.zip"
        shutil.copyfile(trace, dest)
        print(f"  [{key}] attempt {attempt}: captured {dest.name} ({dest.stat().st_size} bytes)")
        return attempt
    return 0


def capture_failure_text() -> bool:
    """Save raw list-reporter output of a failing flaky-selector run for gh fixtures."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        proc = run_spec(SPECS["selector"], "list")
        if proc.returncode != 0:
            (GH / "raw_playwright_output.txt").write_text(proc.stdout + "\n" + proc.stderr)
            print(f"  [gh-log] attempt {attempt}: captured raw failing output")
            return True
    return False


def main() -> int:
    TRACES.mkdir(parents=True, exist_ok=True)
    GH.mkdir(parents=True, exist_ok=True)
    failures = {}
    for key, spec in SPECS.items():
        print(f"collecting failing trace for {spec}")
        failures[key] = collect_trace(key, spec)
    ok_text = capture_failure_text()
    missing = [k for k, n in failures.items() if n == 0]
    print(f"summary: attempts-to-fail={failures}, raw-output-captured={ok_text}")
    if missing or not ok_text:
        print(f"FAILED to observe failures for: {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
