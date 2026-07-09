"""Persistence of parsed JUnit runs into SQLite.

Parsing lives in ``parser.py`` (XML → ``ParsedRun``); this module is the writer
that turns a ``ParsedRun`` into ``test_runs``/``test_cases`` rows. The split keeps
the file-format knowledge separate from the storage knowledge — a second report
format only needs a new parser, not changes here.

There is intentionally no dedup of repeated ingests of the same file (MVP
design — re-ingesting produces a second run row; use ``prune`` to trim).
"""

import logging
from pathlib import Path

from . import parser

logger = logging.getLogger(__name__)


def ingest_file(conn, path: Path, *, commit: bool = True) -> int:
    """Parse and persist one JUnit XML file. Returns the new ``test_runs.id``.

    ``parser.parse_junit_xml`` resolves the path through ``os.path.realpath``
    before reading it (#10). Pass ``commit=False`` to batch many files into one
    transaction (used by ``ingest_directory``); the caller then commits once.
    """
    run = parser.parse_junit_xml(Path(path))

    cur = conn.execute(
        "INSERT INTO test_runs "
        "(suite_name, run_timestamp, total, failures, errors, skipped, source_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run.suite_name, run.run_timestamp, run.total, run.failures,
         run.errors, run.skipped, run.source_file),
    )
    run_id = cur.lastrowid

    try:
        conn.executemany(
            "INSERT INTO test_cases "
            "(run_id, classname, name, file_path, line_number, status, duration_ms, "
            " failure_message, failure_type, stack_trace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, c.classname, c.name, c.file_path, c.line_number, c.status,
                 c.duration_ms, c.failure_message, c.failure_type, c.stack_trace)
                for c in run.cases
            ],
        )
    except Exception:
        # A case failed to insert (e.g. a value rejected only at bind time). Undo
        # this file's run row — and any cases that did land — so a shared batch
        # transaction (ingest_directory, commit=False) is never left with an
        # orphan run that has no cases. Delete cases explicitly first so the FTS
        # delete-trigger fires for any already inserted, then re-raise for the
        # caller to log and skip.
        conn.execute("DELETE FROM test_cases WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM test_runs WHERE id = ?", (run_id,))
        raise

    if commit:
        conn.commit()
    return run_id


def ingest_directory(conn, dir_path: Path) -> tuple[list[int], int]:
    """Recursively ingest every ``*.xml`` under ``dir_path``.

    Returns ``(run_ids, skipped_count)``. Files that fail to parse — or to
    insert — are logged and skipped; this never raises for a bad file, so one
    malformed report cannot abort a whole directory ingest. All successful files
    are committed in a single transaction; a skipped file leaves no rows behind
    (a parse failure happens before any insert, and ``ingest_file`` rolls back
    its own run row if a later case insert fails), so it never pollutes the batch.
    """
    dir_path = Path(dir_path)
    run_ids: list[int] = []
    skipped = 0
    for xml_path in sorted(dir_path.rglob("*.xml")):
        try:
            run_ids.append(ingest_file(conn, xml_path, commit=False))
        except Exception as exc:  # noqa: BLE001 — intentional: skip & continue
            logger.warning("test-history: skipping unparseable file %s: %s", xml_path, exc)
            skipped += 1
            continue
    conn.commit()
    if skipped:
        logger.warning("test-history: ingested %d file(s), skipped %d", len(run_ids), skipped)
    return run_ids, skipped
