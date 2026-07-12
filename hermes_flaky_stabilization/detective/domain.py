"""Shared domain vocabulary — the rules and tunable defaults that more than one
module must agree on, gathered here so they have a single owner and cannot drift.

Mirrors the discipline of the sibling ``hermes-test-history`` plugin's
``domain.py``: only things with more than one caller live here (status names, the
tunable defaults echoed by both the tool schema and the config layer, and the
shared "effective timestamp" SQL fragment). It is kept import-free so importing
it at Hermes startup (``__init__`` references it at module scope) costs nothing.

GPL note: the ``effective_time`` expression below is re-implemented locally on
purpose. ``hermes-test-history`` is GPL-3.0; this plugin only reads its SQLite
*data file* (not a derivative work) and never *imports* its Python modules, so it
stays under its own license. See README's "GPL / data-only boundary".
"""

# ---------------------------------------------------------------------------
# Per-case outcome vocabulary (the ``status`` values stored by test-history)
# ---------------------------------------------------------------------------

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


def fail_statuses(include_errors: bool) -> tuple[str, ...]:
    """Per-case statuses that count as a *failure* for flakiness.

    ``failed`` always counts; ``error`` counts only when ``include_errors`` is
    set (Phase 0 question 2, default yes). ``skipped`` never counts (it is
    excluded from both the pass and fail tallies), and an ``error`` that is *not*
    counted as a failure is likewise excluded from both — it is neither a pass
    nor a fail, so it does not inflate ``runs``.
    """
    return (STATUS_FAILED, STATUS_ERROR) if include_errors else (STATUS_FAILED,)


# ---------------------------------------------------------------------------
# Verdict vocabulary (this plugin's own classification of a test)
# ---------------------------------------------------------------------------

# A test that fails intermittently: enough failures to matter, but also passes.
VERDICT_FLAKY = "flaky"
# A test that fails (>= threshold) and never passes in the window: broken, not flaky.
VERDICT_CONSISTENTLY_FAILING = "consistently_failing"
# Anything below the failure threshold.
VERDICT_STABLE = "stable"
# Returned by the ``is_flaky`` tool when a test has no verdict on record.
VERDICT_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Tunable defaults (mirrored in config.DEFAULT_CONFIG and the tool/CLI layers)
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_DAYS = 14        # detection look-back window
DEFAULT_MIN_FAILS = 3           # minimum failures in the window to be non-stable
DEFAULT_INCLUDE_ERRORS = True   # count `error` status as a failure
DEFAULT_DELIVER = "local"       # cron delivery channel
DEFAULT_SCHEDULE = "0 9 * * *"  # cron schedule (daily 09:00)
DEFAULT_REPORT_SCOPE = "changes-only"  # nightly report scope

# We read another plugin's schema, so we pin to the version we were built against
# and warn if the installed test-history DB reports something different (§3.1).
EXPECTED_SOURCE_SCHEMA_VERSION = 1


def validate_tunables(window_days: int, min_fails: int) -> None:
    """Reject nonsensical detection tunables before they produce wrong verdicts.

    This is a domain invariant, not a CLI concern, so it lives next to the
    defaults it guards: every caller that sets these tunables (the ``scan``
    use-case, ``install-cron``, any future trigger) enforces the same rule.

    ``min_fails`` must be ``>= 1``: with ``min_fails <= 0`` the ``fails >=
    min_fails`` test is always true, so every all-passing (perfectly stable) test
    would be mislabeled *flaky*. ``window_days`` must be ``>= 1``: a zero/negative
    window selects no runs. Raises :class:`ValueError` with a one-line, joined
    message (the tunable labels are kept neutral so the message reads the same
    whether the value came from a flag or from ``config.json``).
    """
    problems = []
    if window_days < 1:
        problems.append(f"window must be >= 1 (got {window_days})")
    if min_fails < 1:
        problems.append(f"min-fails must be >= 1 (got {min_fails})")
    if problems:
        raise ValueError("; ".join(problems))


_CRON_MACROS = frozenset({
    "@yearly", "@annually", "@monthly", "@weekly", "@daily",
    "@midnight", "@hourly", "@reboot",
})
# A cron field is digits, the operators * / , - and (optionally) month/day names.
# Deliberately no whitespace and no shell metacharacters.
_CRON_FIELD_ALLOWED = frozenset(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz*/,-"
)


def validate_cron_schedule(schedule: str) -> None:
    """Reject a schedule that is not a plausible cron expression.

    ``install-cron`` passes the schedule as a single *positional* argument to
    ``hermes cron create``; a value beginning with ``-`` would be parsed as an
    option by that CLI (argument injection), and a real cron field never contains
    whitespace-within-a-field or shell metacharacters. Accepts the standard 5- or
    6-field form and the ``@macro`` shorthands. Kept regex-free so this module
    stays import-free (it is referenced at Hermes startup). Raises
    :class:`ValueError`.
    """
    s = (schedule or "").strip()
    if not s:
        raise ValueError("cron schedule is empty")
    if s.lower() in _CRON_MACROS:
        return
    if s.startswith("-"):
        raise ValueError(
            f"invalid cron schedule {schedule!r}: must not start with '-' "
            "(it would be parsed as a command-line option)"
        )
    fields = s.split()
    if len(fields) not in (5, 6):
        raise ValueError(
            f"invalid cron schedule {schedule!r}: expected 5 or 6 fields "
            f"or an @macro, got {len(fields)}"
        )
    for field in fields:
        if not set(field) <= _CRON_FIELD_ALLOWED:
            raise ValueError(
                f"invalid character in cron field {field!r} of schedule {schedule!r}"
            )


# ---------------------------------------------------------------------------
# Test identity
# ---------------------------------------------------------------------------


def make_test_key(classname, name, file_path=None) -> str:
    """Canonical identity for a test: ``"{classname}::{name}"``.

    When ``classname`` is absent, ``file_path`` becomes the namespace. This
    prevents same-named Jest tests in different files from collapsing together.
    """
    cls = (classname or "").strip()
    nm = (name or "").strip()
    namespace = cls or (file_path or "").strip()
    return f"{namespace}::{nm}"


# ---------------------------------------------------------------------------
# Shared SQL fragments (static text only — never caller input)
# ---------------------------------------------------------------------------


def effective_time(alias: str = "") -> str:
    """SQL for a run's effective timestamp: ``run_timestamp`` else ``ingested_at``.

    ``alias`` is an optional table qualifier *including the trailing dot* (e.g.
    ``"tr."``). The returned text is a static column expression containing no
    caller-supplied values, so callers concatenate it into SQL safely; every
    actual value still binds through ``?``. Re-implemented locally (not imported
    from test-history) to keep the GPL boundary intact.
    """
    return f"COALESCE({alias}run_timestamp, {alias}ingested_at)"
