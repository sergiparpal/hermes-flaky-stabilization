# Trace fixtures

Provenance (plan step 0.5, DP-3 = generate real traces):

- `selector.trace.zip`, `timeout.trace.zip`, `race.trace.zip` — **real** Playwright
  1.60.0 traces (format version 8), captured on 2026-06-12 by running the toy app's
  flaky specs locally with `trace: 'on'` until each produced a genuine failure
  (`fixtures/scripts/collect_traces.py`). Flakiness observed during capture:
  flaky-selector passed before failing on attempt 2; the raw-log capture saw it
  pass 4 times before failing on attempt 5.
- `ambiguous-synthetic.trace.zip` — **synthetic** (built by
  `fixtures/scripts/make_synthetic_trace.py`, added in Phase 2). It mimics the
  version-8 NDJSON layout but describes a deliberately unclassifiable failure, so
  tests can exercise the stage-2 LLM path of the diagnosis pipeline without any of
  the real fixtures consuming the stub.

Each real archive contains `test.trace` (test-runner events incl. the final
`error` event), `0-trace.trace` (context API events, `log` lines,
`frame-snapshot`s with `[TAG, {attrs}, ...children]` DOM encoding plus `[[n,m]]`
delta back-references), `0-trace.network` (HAR-like `resource-snapshot`s; pending
requests carry `time: -1`), `0-trace.stacks`, and `resources/`.
