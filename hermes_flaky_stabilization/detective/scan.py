"""Application/use-case layer: run one detection sweep end-to-end.

``run_scan`` is the single entry point for "run a scan" — the orchestration that
used to live inside ``cli._cmd_scan``. It threads the lower layers together
(validate tunables → window → read test-history → classify → diff vs the previous
snapshot → persist → record an audit row) and returns a :class:`ScanOutcome`
*data* object. It does **no printing**: the CLI (or a test, or any future trigger)
decides how to present the outcome. That keeps the use case reusable and testable
without capturing stdout, and keeps the CLI a thin args→params→render adapter.

Persistence is reached through an injected ``store`` (a :class:`storage.Storage`),
never a global — so a caller fully controls the connection's lifecycle and tests
can pass their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import detect, domain, query, reporting, timeutil


@dataclass
class ScanOutcome:
    """The result of a scan, tagged by ``kind`` so the caller can render it.

    * ``"unavailable"`` — test-history could not be read; ``message`` is the
      actionable reason (no snapshot was touched).
    * ``"no_runs"``     — test-history is reachable but nothing was *counted* in
      the window (empty, or every row uncounted — e.g. all ``error`` with
      ``include_errors`` off); the previous snapshot is intentionally preserved.
      ``on_record`` is how many verdicts were kept; ``meta`` describes the
      (zero-examined) audit row.
    * ``"ok"``          — a fresh snapshot was written. ``verdicts`` is the new
      set, ``newly_flaky``/``newly_resolved`` the diff vs the previous snapshot.
    """

    kind: str
    meta: dict | None = None
    verdicts: list | None = None
    newly_flaky: list | None = None
    newly_resolved: list | None = None
    on_record: int | None = None
    message: str | None = None


def _scan_meta(*, window_days, min_fails, include_errors,
               source_schema_version, tests_examined, flaky_found) -> dict:
    """The scan metadata block shared by the JSON dump and the human report."""
    return {
        "window_days": window_days, "min_fails": min_fails,
        "include_errors": include_errors,
        "source_schema_version": source_schema_version,
        "tests_examined": tests_examined, "flaky_found": flaky_found,
    }


def run_scan(store, *, window_days: int, min_fails: int, include_errors: bool,
             db_path, now: datetime) -> ScanOutcome:
    """Run a detection sweep and persist the result; return a :class:`ScanOutcome`.

    Raises :class:`ValueError` (from :func:`domain.validate_tunables`) for invalid
    tunables — validated first, so a bad value never touches test-history. All
    other outcomes are returned as data, never printed.
    """
    domain.validate_tunables(window_days, min_fails)

    cutoff = timeutil.window_cutoff(now, window_days)
    try:
        read = query.read_test_history(db_path, cutoff)
    except query.TestHistoryUnavailable as exc:
        return ScanOutcome(kind="unavailable", message=str(exc))

    # Classify, then drop verdicts with zero counted runs: a test whose window
    # holds only uncounted rows (e.g. every run is an `error` with
    # include_errors off — a CI outage) carries no evidence and must never be
    # persisted as "stable with 0 runs". A scan left with nothing counted at
    # all is then treated exactly like an empty window below.
    verdicts = [
        v for v in detect.compute_verdicts(read.rows, now, window_days,
                                           min_fails, include_errors)
        if v.runs > 0
    ]

    if not verdicts:
        # Reachable but nothing counted in the window (empty — e.g. every run aged
        # past it — or every row uncounted). Do NOT overwrite the snapshot with an
        # empty one: that would make every previously-flaky test report as "no
        # longer flaky" and is_flaky answer "unknown" for everything. Keep the
        # prior verdicts; record an audit row noting nothing was examined.
        on_record = sum(store.count_by_status().values())
        store.record_scan_run(
            window_days=window_days, min_fails=min_fails, include_errors=include_errors,
            source_schema_version=read.source_schema_version,
            tests_examined=0, flaky_found=0,
        )
        meta = _scan_meta(
            window_days=window_days, min_fails=min_fails, include_errors=include_errors,
            source_schema_version=read.source_schema_version,
            tests_examined=0, flaky_found=0,
        )
        return ScanOutcome(kind="no_runs", meta=meta, on_record=on_record,
                           verdicts=[], newly_flaky=[], newly_resolved=[])

    new_flaky_keys = {v.test_key for v in verdicts if v.status == domain.VERDICT_FLAKY}

    prev_flaky_keys = store.read_flaky_keys()          # BEFORE replace (for the diff)
    store.replace_verdicts(verdicts)
    store.record_scan_run(
        window_days=window_days, min_fails=min_fails, include_errors=include_errors,
        source_schema_version=read.source_schema_version,
        tests_examined=len(verdicts), flaky_found=len(new_flaky_keys),
    )

    newly_flaky, newly_resolved = reporting.flaky_changes(prev_flaky_keys, new_flaky_keys)
    meta = _scan_meta(
        window_days=window_days, min_fails=min_fails, include_errors=include_errors,
        source_schema_version=read.source_schema_version,
        tests_examined=len(verdicts), flaky_found=len(new_flaky_keys),
    )
    return ScanOutcome(kind="ok", meta=meta, verdicts=verdicts,
                       newly_flaky=newly_flaky, newly_resolved=newly_resolved)
