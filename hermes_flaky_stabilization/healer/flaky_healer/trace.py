"""Playwright trace.zip parser (stdlib zipfile + NDJSON; plan Phase 2.1).

Built against trace format v8 (Playwright 1.60) and tolerant of nearby
versions: every event field access is defensive. A trace.zip contains
``test.trace`` (test-runner events + final pretty ``error`` event),
``N-trace.trace`` (context API ``before``/``after``/``log``/``frame-snapshot``
events) and ``N-trace.network`` (HAR-like ``resource-snapshot``; a pending
request carries ``time: -1`` / no response status).
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .shapes import shape
from .zipsafe import ZipBudget, ZipLimitError, guard_entry_count

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SELECTOR_ID_RE = re.compile(r"#([\w-]+)")
_ERROR_NAME_RE = re.compile(r"\b([A-Z][A-Za-z]*Error)\b")

# test-runner bookkeeping classes that are not browser actions
_NON_ACTION_CLASSES = {"Test", "Tracing", "LocalUtils"}


@dataclass
class TraceAction:
    call_id: str
    api_name: str  # e.g. "click", "goto", "expect.toHaveText"
    klass: str = ""
    selector: str | None = None
    timeout_ms: float | None = None
    start_ms: float | None = None
    end_ms: float | None = None
    error_name: str | None = None
    error_message: str | None = None
    timed_out: bool = False
    call_log: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.error_name is not None


@dataclass
class NetworkRequest:
    url: str
    method: str = "GET"
    status: int | None = None
    duration_ms: float | None = None
    pending: bool = False


@dataclass
class TraceSummary:
    framework: str = "playwright"
    trace_version: int | None = None
    failing_action: str | None = None
    selector: str | None = None
    timeout_ms: float | None = None
    error_name: str | None = None
    error_message: str | None = None
    test_error_message: str | None = None
    timed_out: bool = False
    selector_resolved: bool = False
    call_log: list[str] = field(default_factory=list)
    actions: list[TraceAction] = field(default_factory=list)
    requests: list[NetworkRequest] = field(default_factory=list)
    pending_requests: list[NetworkRequest] = field(default_factory=list)
    dom_testids: dict[str, dict] = field(default_factory=dict)
    dom_ids: dict[str, dict] = field(default_factory=dict)
    testid_on_target: str | None = None

    def brief(self) -> dict:
        """JSON-safe condensed view for tool output."""
        return {
            "framework": self.framework,
            "trace_version": self.trace_version,
            "failing_action": self.failing_action,
            "selector": self.selector,
            "timeout_ms": self.timeout_ms,
            "error_name": self.error_name,
            "error_message": (self.error_message or "")[:500],
            "timed_out": self.timed_out,
            "selector_resolved": self.selector_resolved,
            "testid_on_target": self.testid_on_target,
            "dom_testids": sorted(self.dom_testids),
            "pending_requests": [
                {"url": r.url, "method": r.method, "status": r.status}
                for r in self.pending_requests
            ],
            "call_log": self.call_log[-20:],
            "actions_total": len(self.actions),
        }


def _strip_ansi(text: str) -> str:
    # The regex scan dominates on long strings; skip it when there's no ESC to strip.
    return _ANSI_RE.sub("", text) if "\x1b" in text else text


def _walk_html(node):
    """Yield (tag, attrs) for every element node in a snapshot html tree.

    Nodes look like ``[TAG, {attrs}, ...children]``; delta back-references
    like ``[[1, 26]]`` contain no strings and are skipped safely.

    Iterative with an explicit stack (preserving pre-order, left-to-right
    traversal): a trace.zip is untrusted, and a deeply nested snapshot (a
    ~1000-level DOM fits trivially inside the zip budget) must not blow the
    interpreter recursion limit and kill the whole parse.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current, list) or not current:
            continue
        if isinstance(current[0], str):
            attrs = current[1] if len(current) > 1 and isinstance(current[1], dict) else None
            yield current[0], (attrs or {})
            children = current[2:] if attrs is not None else current[1:]
            stack.extend(reversed(children))
        else:
            stack.extend(reversed(current))


def _api_name(method: str, title: str | None) -> str:
    if method == "expect" and title:
        m = re.search(r'"([^"]+)"', title)
        if m:
            return f"expect.{m.group(1)}"
    return method


def _resolve_error_name(error: dict, result: dict, message: str) -> str:
    name = str(error.get("name") or "").strip()
    if name and name not in ("Expect", "Error"):
        return name
    for source in (str(error.get("stack") or ""), message):
        m = _ERROR_NAME_RE.search(source.split("\n", 1)[0])
        if m:
            return m.group(1)
    if result.get("timedOut"):
        return "TimeoutError"
    return name or "Error"


def _iter_ndjson(budget, zf, entries):
    """Yield each decoded JSON object from the NDJSON ``entries``, in sorted order.

    Blank lines, lines that fail to parse, and payloads that don't decode to a
    ``dict`` are skipped — the exact scaffolding the trace- and network-entry
    scan loops each carried verbatim. Reads go through ``budget.iter_lines`` so
    the shared ``ZipBudget`` (a zip-bomb decompressed-byte guard, see
    flaky_healer.zipsafe) is accounted identically for both scans; routing both
    through one generator is what keeps that guard from drifting between them.
    The caller passes the SAME ``budget`` across successive calls, so the byte
    budget accumulates across every entry exactly as the single ``budget``
    threaded through both inline loops did.
    """
    for entry in sorted(entries):
        for raw_line in budget.iter_lines(zf, entry):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                ev = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                yield ev


def parse_trace(path: str | Path) -> TraceSummary:
    path = Path(path)
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, FileNotFoundError, IsADirectoryError) as exc:
        raise ValueError(f"not a readable Playwright trace.zip: {path} ({exc})") from exc

    summary = TraceSummary()
    calls: dict[str, TraceAction] = {}
    order: list[str] = []

    with zf:
        # A trace.zip is untrusted input; bound entry count and decompressed size
        # so a decompression bomb cannot exhaust memory (see flaky_healer.zipsafe).
        try:
            guard_entry_count(zf)
            budget = ZipBudget()
            names = zf.namelist()
            trace_entries = [n for n in names if n.endswith(".trace")]
            network_entries = [n for n in names if n.endswith(".network")]
            if not trace_entries:
                raise ValueError(f"no .trace entries inside {path.name}; not a Playwright trace")

            for ev in _iter_ndjson(budget, zf, trace_entries):
                _ingest_trace_event(ev, summary, calls, order)

            for ev in _iter_ndjson(budget, zf, network_entries):
                _ingest_network_event(ev, summary)
        except ZipLimitError as exc:
            raise ValueError(f"trace.zip rejected: {exc}") from exc

    if not calls and summary.trace_version is None:
        raise ValueError(f"{path.name} contains no recognizable Playwright trace events")

    summary.actions = [calls[c] for c in order]
    _finalize(summary)
    return summary


def _ingest_context_options(ev: dict, summary: TraceSummary, calls: dict, order: list) -> None:
    version = ev.get("version")
    if isinstance(version, int):
        summary.trace_version = max(summary.trace_version or 0, version)


def _ingest_before(ev: dict, summary: TraceSummary, calls: dict, order: list) -> None:
    if ev.get("class") in _NON_ACTION_CLASSES:
        return
    call_id = ev.get("callId")
    if not call_id:  # unidentifiable action — cannot correlate its after/log events
        return
    if call_id in calls:  # duplicate before for the same id: keep the first
        return
    params = ev.get("params")
    if not isinstance(params, dict):
        params = {}
    selector = params.get("selector")
    timeout = params.get("timeout")
    action = TraceAction(
        call_id=call_id,
        api_name=_api_name(ev.get("method") or "", ev.get("title")),
        klass=ev.get("class") or "",
        selector=selector if isinstance(selector, str) else None,
        timeout_ms=timeout if isinstance(timeout, int | float) else None,
        start_ms=ev.get("startTime"),
        params=params,
    )
    calls[call_id] = action
    order.append(call_id)


def _ingest_after(ev: dict, summary: TraceSummary, calls: dict, order: list) -> None:
    action = calls.get(ev.get("callId") or "")
    if action is None:
        return
    action.end_ms = ev.get("endTime")
    error = ev.get("error")
    if not isinstance(error, dict):
        error = {}
    if isinstance(error.get("error"), dict):  # older traces nest it once more
        error = error["error"]
    result = ev.get("result")
    if not isinstance(result, dict):
        result = {}
    if error:
        message = str(error.get("message") or "")
        action.error_message = _strip_ansi(message)
        action.error_name = _resolve_error_name(error, result, message)
        action.timed_out = bool(
            result.get("timedOut")
            or ("timeout" in message.lower() and "exceeded" in message.lower())
        )
    log = result.get("log")
    if isinstance(log, list):
        for line in log:
            action.call_log.append(_strip_ansi(str(line)).strip())


def _ingest_log(ev: dict, summary: TraceSummary, calls: dict, order: list) -> None:
    action = calls.get(ev.get("callId") or "")
    if action is not None:
        action.call_log.append(_strip_ansi(str(ev.get("message") or "")).strip())


def _ingest_frame_snapshot(ev: dict, summary: TraceSummary, calls: dict, order: list) -> None:
    snapshot = ev.get("snapshot")
    if not isinstance(snapshot, dict):
        return
    for _tag, attrs in _walk_html(snapshot.get("html")):
        testid = attrs.get("data-testid")
        if testid and testid not in summary.dom_testids:
            summary.dom_testids[testid] = dict(attrs)
        el_id = attrs.get("id")
        if el_id and el_id not in summary.dom_ids:
            # Only the element's data-testid is read downstream
            # (_match_testid_for_selector); storing that alone avoids copying
            # every id-bearing element's full attribute dict on a large DOM.
            summary.dom_ids[el_id] = {"data-testid": testid}


def _ingest_error(ev: dict, summary: TraceSummary, calls: dict, order: list) -> None:
    message = _strip_ansi(str(ev.get("message") or ""))
    if message and not summary.test_error_message:
        summary.test_error_message = message


# Dispatch table replacing what was one long if/elif chain over ``ev["type"]``.
# A table keeps each event type's handling in a single, independently testable
# place and makes an unrecognized type a no-op (the old chain's fall-through),
# so adding/removing an event type can't accidentally reorder the others.
_TRACE_EVENT_HANDLERS = {
    "context-options": _ingest_context_options,
    "before": _ingest_before,
    "after": _ingest_after,
    "log": _ingest_log,
    "frame-snapshot": _ingest_frame_snapshot,
    "error": _ingest_error,
}


def _ingest_trace_event(ev: dict, summary: TraceSummary, calls: dict, order: list) -> None:
    handler = _TRACE_EVENT_HANDLERS.get(ev.get("type"))
    if handler is not None:
        handler(ev, summary, calls, order)


def _ingest_network_event(ev: dict, summary: TraceSummary) -> None:
    if ev.get("type") != "resource-snapshot":
        return
    snap = ev.get("snapshot")
    if not isinstance(snap, dict):
        return
    request = snap.get("request") if isinstance(snap.get("request"), dict) else {}
    response = snap.get("response") if isinstance(snap.get("response"), dict) else {}
    duration = snap.get("time")
    status = response.get("status")
    pending = (isinstance(duration, int | float) and duration < 0) or not status
    summary.requests.append(
        NetworkRequest(
            url=request.get("url") or "",
            method=request.get("method") or "GET",
            status=status if status else None,
            duration_ms=duration,
            pending=pending,
        )
    )


def _match_testid_for_selector(summary: TraceSummary, selector: str | None) -> str | None:
    """Prove the failing selector's target carries a data-testid (plan §4.3.2).

    Exact id match first; then shape-normalized id match (rotating ids like
    btn-1f9c / btn-59c2 normalize to btn-<h>) — but only when unambiguous.

    Only a *standalone* ``#id`` selector is considered: a compound selector like
    ``#app #submit`` would have us rewrite to the wrong (ancestor) element, so we
    decline rather than risk an assertion-weakening fix.
    """
    if not isinstance(selector, str):
        return None
    m = _SELECTOR_ID_RE.fullmatch(selector.strip())
    if not m:
        return None
    sel_id = m.group(1)
    exact = summary.dom_ids.get(sel_id)
    if exact is not None:
        return exact.get("data-testid")
    sel_shape = shape(sel_id)
    candidates = {
        attrs.get("data-testid")
        for el_id, attrs in summary.dom_ids.items()
        if shape(el_id) == sel_shape and attrs.get("data-testid")
    }
    if len(candidates) == 1:
        return candidates.pop()
    return None


def _finalize(summary: TraceSummary) -> None:
    failed = [a for a in summary.actions if a.failed]
    failing = failed[-1] if failed else None
    if failing is not None:
        summary.failing_action = failing.api_name
        summary.selector = failing.selector
        summary.timeout_ms = failing.timeout_ms
        summary.error_name = failing.error_name
        summary.error_message = failing.error_message
        summary.timed_out = failing.timed_out
        summary.call_log = list(failing.call_log)
        summary.selector_resolved = any(
            "resolved to" in line for line in failing.call_log
        )
        summary.testid_on_target = _match_testid_for_selector(summary, failing.selector)
    summary.pending_requests = [r for r in summary.requests if r.pending]
