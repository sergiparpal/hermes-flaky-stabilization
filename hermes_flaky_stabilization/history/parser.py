"""JUnit XML parsing — turns a report file into ``ParsedRun``/``ParsedCase``.

JUnit XML has three live dialects in the wild (pytest, jest-junit, maven
surefire), so the parser is deliberately defensive: it accepts both
``<testsuites>`` and ``<testsuite>`` roots, tolerates missing attributes, and
falls back to file mtime when a run timestamp is absent.

This module is pure: it reads a file and returns dataclasses, with no SQLite or
storage dependency. Persistence of the returned ``ParsedRun`` lives in
``ingest.py``; the dataclasses are the seam between the two. Keeping parsing
isolated is also where a second report format (e.g. Allure JSON) would slot in —
a sibling parser producing the same ``ParsedRun`` — without touching the writer.
"""

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .timeutil import iso_to_naive_utc

logger = logging.getLogger(__name__)

# Reject XML larger than this before parsing. A full-DOM parse holds the entire
# tree in memory, so an oversized report is a memory-exhaustion DoS regardless of
# its contents. Tunable for unusually large CI artifacts.
_MAX_XML_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Parsed representations
# ---------------------------------------------------------------------------


@dataclass
class ParsedCase:
    name: str
    status: str                              # 'passed' | 'failed' | 'error' | 'skipped'
    classname: str | None = None
    file_path: str | None = None
    line_number: int | None = None
    duration_ms: float | None = None
    failure_message: str | None = None
    failure_type: str | None = None
    stack_trace: str | None = None


@dataclass
class ParsedRun:
    suite_name: str
    source_file: str
    run_timestamp: str | None = None
    total: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    cases: list[ParsedCase] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small attribute helpers (all defensive: empty string -> None)
# ---------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


# SQLite stores INTEGER as a signed 64-bit value. A coerced int outside this
# range can't be bound — sqlite3 raises OverflowError at execute time, and would
# do so *after* the run row is already staged (see ingest.ingest_file). Reject
# such values here, at parse time, so a single absurd attribute neither crashes
# the bind nor strands a half-written run.
_INT64_MIN, _INT64_MAX = -(2 ** 63), 2 ** 63 - 1


def _to_int(value: str | None) -> int | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        result = int(float(value))        # tolerate "3", "3.0"
    except (ValueError, TypeError, OverflowError):
        return None                       # non-numeric, or non-finite ("1e999"/"inf"/"nan")
    if not (_INT64_MIN <= result <= _INT64_MAX):
        return None                       # finite but too big for a SQLite INTEGER ("1e308")
    return result


def _to_float(value: str | None) -> float | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def _normalize_iso(value: str | None) -> str | None:
    """Normalize a timestamp string to naive (UTC) ISO-8601 seconds precision.

    Returns ``None`` when the string cannot be parsed: SQLite's ``datetime()``
    can't parse what ``fromisoformat`` rejects either, so keeping such a value
    would make the run invisible to every time-windowed query. Returning ``None``
    lets the caller fall back to file mtime instead. The ISO parsing itself lives
    in ``timeutil.iso_to_naive_utc``, shared with the query/CLI layer.
    """
    value = _clean(value)
    if value is None:
        return None
    dt = iso_to_naive_utc(value)
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _mtime_iso(path: Path) -> str | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return (
        datetime.fromtimestamp(mtime, UTC)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_case(tc, suite_file: str | None) -> ParsedCase:
    classname = _clean(tc.get("classname"))
    name = _clean(tc.get("name")) or "<unknown>"
    file_path = _clean(tc.get("file")) or suite_file
    line_number = _to_int(tc.get("line"))
    duration = _to_float(tc.get("time"))
    duration_ms = duration * 1000.0 if duration is not None else None

    status, message, ftype, trace = "passed", None, None, None
    # Precedence: error > failure > skipped. A testcase normally carries only one.
    err = tc.find("error")
    fail = tc.find("failure")
    skip = tc.find("skipped")
    if err is not None:
        status, el = "error", err
    elif fail is not None:
        status, el = "failed", fail
    elif skip is not None:
        status, el = "skipped", skip
    else:
        el = None
    if el is not None:
        message = _clean(el.get("message"))
        ftype = _clean(el.get("type"))
        trace = _clean(el.text)

    return ParsedCase(
        name=name,
        status=status,
        classname=classname,
        file_path=file_path,
        line_number=line_number,
        duration_ms=duration_ms,
        failure_message=message,
        failure_type=ftype,
        stack_trace=trace,
    )


def _aggregate_counts(root, suites, cases: list[ParsedCase]) -> tuple[int, int, int, int]:
    """Resolve run-level counts: prefer per-suite attribute sums, then the root
    aggregate attributes, then counts derived from the parsed cases."""

    def suite_sum(key: str) -> int | None:
        # Trust the suite attributes only when EVERY suite carries the key: a
        # partial sum would undercount (suite A tests="3" plus an attribute-less
        # suite B with 4 cases must not yield total==3 for a 7-case run) while
        # suppressing the case-derived fallback below.
        values = [_to_int(s.get(key)) for s in suites]
        if not values or any(v is None for v in values):
            return None
        return sum(values)

    def resolve(key: str, status: str | tuple[str, ...]) -> int:
        val = suite_sum(key)
        if val is None:
            val = _to_int(root.get(key))
        if val is None:
            wanted = (status,) if isinstance(status, str) else status
            val = sum(1 for c in cases if c.status in wanted)
        return val

    total = resolve("tests", ("passed", "failed", "error", "skipped"))
    failures = resolve("failures", "failed")
    errors = resolve("errors", "error")
    skipped = resolve("skipped", "skipped")
    return total, failures, errors, skipped


def _parse_xml_root(path: Path):
    """Parse a JUnit XML file into an ElementTree root element, hardened against
    entity-expansion ("billion laughs"/quadratic-blowup) and external-DTD/XXE
    attacks.

    Legitimate JUnit XML never declares a DTD, so we forbid any DOCTYPE outright:
    the ``StartDoctypeDeclHandler`` fires *before* expat expands a single entity,
    which makes the billion-laughs DoS impossible while still letting every real
    report through. ``SetParamEntityParsing(...NEVER)`` additionally refuses
    external-DTD-subset retrieval. A size cap guards plain oversized files.

    ``xml.etree``/``expat`` are imported here, not at module scope, so importing
    the plugin at Hermes startup stays cheap (hard-constraint #2). Malformed XML
    raises ``ExpatError`` and an oversized file or a DTD raises ``ValueError`` —
    both are caught and skipped by ``ingest.ingest_directory``.
    """
    import xml.etree.ElementTree as ET  # lazy import (hard-constraint #2)
    from xml.parsers import expat

    size = path.stat().st_size
    if size > _MAX_XML_BYTES:
        raise ValueError(f"XML file too large to ingest ({size} bytes > {_MAX_XML_BYTES})")

    def _forbid_dtd(name, system_id, public_id, has_internal_subset):
        raise ValueError("DTD/DOCTYPE is not allowed in JUnit XML")

    builder = ET.TreeBuilder()
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _forbid_dtd
    parser.StartElementHandler = lambda tag, attrib: builder.start(tag, attrib)
    parser.EndElementHandler = lambda tag: builder.end(tag)
    parser.CharacterDataHandler = builder.data
    with open(path, "rb") as fh:
        parser.ParseFile(fh)
    return builder.close()


def _collect_cases(root, suites) -> list[ParsedCase]:
    """Parse every ``<testcase>``, preferring suite-scoped collection.

    Falls back to a document-wide search when the structured walk finds nothing,
    so testcases nested in an unexpected shape are still captured.
    """
    cases: list[ParsedCase] = []
    for suite in (suites or [root]):
        suite_file = _clean(suite.get("file"))
        for tc in suite.findall("testcase"):
            cases.append(_parse_case(tc, suite_file))
    # Last-ditch: testcases nested anywhere, if nothing matched above.
    if not cases:
        for tc in root.findall(".//testcase"):
            cases.append(_parse_case(tc, None))
    return cases


def _resolve_suite_name(root, suites, path: Path) -> str:
    """Suite name: root ``@name``, else the first suite's ``@name``, else the filename."""
    return (
        _clean(root.get("name"))
        or (_clean(suites[0].get("name")) if suites else None)
        or path.name
    )


def _resolve_run_timestamp(root, suites, path: Path) -> str | None:
    """Root timestamp, then first suite timestamp, then the file mtime."""
    root_timestamp = _normalize_iso(root.get("timestamp"))
    if root_timestamp:
        return root_timestamp
    for suite in suites:
        ts = _normalize_iso(suite.get("timestamp"))
        if ts:
            return ts
    return _mtime_iso(path)


def parse_junit_xml(path: Path) -> ParsedRun:
    """Parse a single JUnit XML file into a ``ParsedRun``.

    Raises on unreadable/malformed/oversized XML or a forbidden DTD — callers
    that ingest many files (``ingest.ingest_directory``) catch and skip.
    """
    path = Path(os.path.realpath(str(path)))   # resolve once, before access (#10)
    root = _parse_xml_root(path)

    if root.tag == "testsuite":
        # The root suite plus any <testsuite> nested inside it. ``suites`` drives
        # case collection and must include the nested suites, or their <testcase>
        # children are silently dropped (each case is collected from its own
        # direct parent, so listing every suite once never double-counts cases).
        # ``top_suites`` (count aggregation) stays the root alone, whose
        # attributes are the suite-level totals.
        suites = [root] + root.findall(".//testsuite")
        top_suites = [root]
    else:
        # ``suites`` (any depth) drives case collection; ``top_suites`` (direct
        # children only) drives the count aggregation, so nested <testsuite>
        # elements are not double-counted in the run totals.
        suites = root.findall(".//testsuite")
        top_suites = root.findall("testsuite")

    cases = _collect_cases(root, suites)

    # Reject documents that are not JUnit XML at all (e.g. a stray HTML/XML
    # file): require a recognizable root or at least one parsed case. Without
    # this, any well-formed XML would be ingested as an empty, useless run row.
    if root.tag not in ("testsuite", "testsuites") and not suites and not cases:
        raise ValueError(f"not a JUnit XML document (root=<{root.tag}>, no testcases)")

    suite_name = _resolve_suite_name(root, suites, path)
    run_timestamp = _resolve_run_timestamp(root, suites, path)
    total, failures, errors, skipped = _aggregate_counts(root, top_suites, cases)

    return ParsedRun(
        suite_name=suite_name,
        source_file=str(path),                 # already resolved above (#10)
        run_timestamp=run_timestamp,
        total=total,
        failures=failures,
        errors=errors,
        skipped=skipped,
        cases=cases,
    )
