#!/usr/bin/env python3
"""Build fixtures/gh/run_logs.zip — a realistic GitHub Actions run-logs archive.

Layout mirrors what `GET /repos/{o}/{r}/actions/runs/{id}/logs` returns: one
folder per job, one `N_Step name.txt` file per step, every line prefixed with
an ISO-8601 timestamp. The "Run Playwright tests" step embeds the real failing
output captured from the toy app (raw_playwright_output.txt) inside ~2 MB of
realistic runner/npm noise, so the log pre-filter has something honest to dig
through.

Usage: python3 fixtures/gh/make_run_logs.py
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw_playwright_output.txt"
OUT = HERE / "run_logs.zip"

BASE_TS = datetime(2026, 6, 11, 22, 10, 31, tzinfo=UTC)


class Stamper:
    """Monotonic fake-timestamp prefixer (GH log lines carry these)."""

    def __init__(self, start: datetime):
        self.now = start

    def lines(self, body: str, step_ms: int = 7) -> str:
        out = []
        for line in body.splitlines():
            self.now += timedelta(milliseconds=step_ms)
            out.append(f"{self.now.strftime('%Y-%m-%dT%H:%M:%S.%f')}0Z {line}")
        return "\n".join(out) + "\n"


def npm_noise(stamper: Stamper, packages: int = 16000) -> str:
    """~2 MB of believable npm/runner output."""
    chunks = [
        "npm warn deprecated inflight@1.0.6: This module is not supported",
        "npm http fetch GET 200 https://registry.npmjs.org/typescript 41ms (cache miss)",
    ]
    for i in range(packages):
        chunks.append(
            f"npm http fetch GET 200 https://registry.npmjs.org/pkg-{i % 977}/-/pkg-{i % 977}-"
            f"{1 + i % 9}.{i % 20}.{i % 10}.tgz {30 + i % 400}ms (cache miss)"
        )
        if i % 50 == 0:
            chunks.append(f"npm timing idealTree:node_modules/pkg-{i % 977} done in {i % 90}ms")
    chunks.append("added 1243 packages in 92s")
    return stamper.lines("\n".join(chunks), step_ms=2)


def main() -> None:
    raw = RAW.read_text() if RAW.is_file() else FALLBACK_FAILURE
    files: dict[str, str] = {}

    s = Stamper(BASE_TS)
    files["e2e-tests/1_Set up job.txt"] = s.lines(
        "\n".join(
            [
                "Current runner version: '2.325.0'",
                "Operating System: Ubuntu 24.04.2 LTS",
                "Runner Image: ubuntu-24.04 Version: 20260605.1.0",
                "Prepare workflow directory",
                "Prepare all required actions",
                "Getting action download info",
                "Download action repository 'actions/checkout@v4'",
                "Complete job name: e2e-tests",
            ]
        )
    )
    files["e2e-tests/2_Checkout.txt"] = s.lines(
        "\n".join(
            [
                "Run actions/checkout@v4",
                "Syncing repository: acme/webshop",
                "Getting Git version info",
                "Fetching the repository",
                "Determining the checkout info",
                "Checking out the ref",
                "/usr/bin/git checkout --progress --force refs/remotes/pull/871/merge",
                "HEAD is now at a1b2c3d Add cart badge counter",
            ]
        )
    )
    files["e2e-tests/3_Install dependencies.txt"] = npm_noise(s)
    files["e2e-tests/4_Run Playwright tests.txt"] = s.lines(
        "Run npx playwright test\n" + raw + "\n##[error]Process completed with exit code 1."
    )
    files["e2e-tests/5_Complete job.txt"] = s.lines(
        "Cleaning up orphan processes\nTerminating orphan process: pid (2243) (node)"
    )

    s2 = Stamper(BASE_TS - timedelta(seconds=6))
    files["lint/1_Set up job.txt"] = s2.lines(
        "Current runner version: '2.325.0'\nComplete job name: lint"
    )
    files["lint/2_Run eslint.txt"] = s2.lines(
        "\n".join(["Run npx eslint .", "", "> webshop@2.4.1 lint", "> eslint .", ""])
    )

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)

    raw_total = sum(len(b.encode()) for b in files.values())
    print(f"wrote {OUT.name}: {OUT.stat().st_size} bytes compressed, {raw_total} bytes raw")


FALLBACK_FAILURE = """
Running 1 test using 1 worker

  x  1 [chromium] > tests/flaky-selector.spec.ts:3:5 > submits the order form (2.3s)

  1) [chromium] > tests/flaky-selector.spec.ts:3:5 > submits the order form

    TimeoutError: locator.click: Timeout 2000ms exceeded.
    Call log:
      - waiting for locator('#btn-1f9c')

       3 | test('submits the order form', async ({ page }) => {
       4 |   await page.goto('/');
    >  5 |   await page.locator('#btn-1f9c').click({ timeout: 2000 });
         |                                   ^
       6 |   await expect(page.locator('#submit-result')).toHaveText('submitted');

        at /home/runner/work/webshop/webshop/tests/flaky-selector.spec.ts:5:35

  1 failed
"""


if __name__ == "__main__":
    main()
