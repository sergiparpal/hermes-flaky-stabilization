#!/usr/bin/env python3
"""Build fixtures/traces/ambiguous-synthetic.trace.zip.

A hand-crafted trace mimicking the v8 NDJSON layout whose failure deliberately
matches NO stage-1 heuristic (no timeout, no env/infra markers, element
resolved, no pending network) so diagnosis tests can exercise the stage-2 LLM
path. Documented as synthetic in fixtures/traces/README.md.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "traces" / "ambiguous-synthetic.trace.zip"

TEST_TRACE = [
    {
        "version": 8,
        "type": "context-options",
        "origin": "testRunner",
        "browserName": "",
        "playwrightVersion": "1.60.0",
        "options": {},
        "platform": "linux",
        "wallTime": 1781270000000,
        "monotonicTime": 1000.0,
        "sdkLanguage": "javascript",
        "testTimeout": 15000,
    },
    {
        "type": "before",
        "callId": "hook@1",
        "stepId": "hook@1",
        "startTime": 1010.0,
        "class": "Test",
        "method": "hook",
        "title": "Before Hooks",
        "params": {},
        "stack": [],
    },
    {"type": "after", "callId": "hook@1", "endTime": 1200.0},
    {
        "type": "error",
        "message": "Error: widget desync (code 0xWAT): rendered checksum mismatch",
        "stack": [{"file": "/repo/tests/widget.spec.ts", "line": 9, "column": 12}],
    },
]

CONTEXT_TRACE = [
    {
        "version": 8,
        "type": "context-options",
        "origin": "library",
        "browserName": "chromium",
        "options": {},
        "platform": "linux",
        "wallTime": 1781270000100,
        "monotonicTime": 1100.0,
        "sdkLanguage": "javascript",
    },
    {
        "type": "before",
        "callId": "call@8",
        "startTime": 1150.0,
        "class": "Frame",
        "method": "goto",
        "params": {"url": "/", "timeout": 0, "waitUntil": "load"},
        "pageId": "page@1",
    },
    {"type": "after", "callId": "call@8", "endTime": 1180.0, "result": {"response": "<Response>"}},
    {
        "type": "before",
        "callId": "call@10",
        "startTime": 1200.0,
        "class": "Frame",
        "method": "click",
        "params": {"selector": "#widget", "strict": True},
        "pageId": "page@1",
    },
    {
        "type": "log",
        "callId": "call@10",
        "time": 1210.0,
        "message": "locator resolved to <div id=\"widget\">…</div>",
    },
    {
        "type": "after",
        "callId": "call@10",
        "endTime": 1300.0,
        "error": {
            "name": "",
            "message": "widget desync (code 0xWAT): rendered checksum mismatch",
            "stack": "Error: widget desync (code 0xWAT)\n    at Object.click (/repo/x.js:1:1)",
        },
    },
    {
        "type": "frame-snapshot",
        "snapshot": {
            "callId": "call@10",
            "snapshotName": "after@call@10",
            "pageId": "page@1",
            "frameUrl": "http://127.0.0.1:4173/",
            "doctype": "html",
            "html": [
                "HTML",
                {},
                ["HEAD", {}, ["TITLE", {}, "Widget"]],
                ["BODY", {}, ["DIV", {"id": "widget"}, "…"]],
            ],
            "viewport": {"width": 1280, "height": 720},
            "timestamp": 1300.0,
        },
    },
]

NETWORK = [
    {
        "type": "resource-snapshot",
        "snapshot": {
            "pageref": "page@1",
            "startedDateTime": "2026-06-12T00:00:00.100Z",
            "time": 12.5,
            "request": {"method": "GET", "url": "http://127.0.0.1:4173/", "headers": []},
            "response": {"status": 200, "headers": []},
        },
    }
]


def ndjson(events) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test.trace", ndjson(TEST_TRACE))
        zf.writestr("0-trace.trace", ndjson(CONTEXT_TRACE))
        zf.writestr("0-trace.network", ndjson(NETWORK))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
