"""Shared domain vocabulary — the rules and tunable defaults that more than one
module must agree on, gathered here so they have a single owner and cannot drift.

Three kinds of thing live here, and *only* things that were otherwise duplicated:

* the definition of a non-passing outcome (``FAIL_STATUSES`` and its SQL form),
  used by the query layer and the test fixtures;
* the default/bound values that appear in both the JSON tool schemas
  (``__init__``) and the query coercion (``queries``), and the config defaults
  mirrored by ``storage.DEFAULT_CONFIG``;
* ``effective_time`` — the ``COALESCE(run_timestamp, ingested_at)`` "a run's
  timestamp is its reported time, else when we ingested it" rule that the query
  and CLI layers compare, sort, and prune on.

Query-local constants that are *not* shared (e.g. ``queries._MAX_OFFENDERS``,
``queries._MAX_INPUT_LEN``) deliberately stay in their module — this file is for
things with more than one caller, not a dumping ground for every literal.

Kept import-free so importing it at Hermes startup (``__init__`` references it at
module scope) costs nothing.
"""

# ---------------------------------------------------------------------------
# Outcome vocabulary
# ---------------------------------------------------------------------------

# What counts as a non-passing outcome for failure-history analysis.
FAIL_STATUSES = ("failed", "error")

# The same tuple rendered as a SQL list literal, for ``status IN (...)`` clauses.
# These are fixed internal constants, never caller input, so concatenating them
# into SQL as a *static fragment* (the values still always go through ``?``)
# keeps the parameterized-SQL rule intact — same technique as ``queries._CASE_COLS``.
FAIL_STATUSES_SQL = "(" + ", ".join(f"'{s}'" for s in FAIL_STATUSES) + ")"


# ---------------------------------------------------------------------------
# Tool-argument defaults / bounds (mirrored in the JSON tool schemas)
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 10        # test_failure_lookup: failures returned
MIN_LIMIT = 1
MAX_LIMIT = 50

DEFAULT_MIN_FAILURES = 1  # module_failure_history: minimum failures to qualify


# ---------------------------------------------------------------------------
# Config-tunable defaults (mirrored in storage.DEFAULT_CONFIG)
# ---------------------------------------------------------------------------

DEFAULT_LOOKBACK_DAYS = 30          # module_failure_history window when `since` omitted
DEFAULT_MAX_STACK_TRACE_CHARS = 500  # per-failure stack-trace truncation length


# ---------------------------------------------------------------------------
# Shared SQL fragments
# ---------------------------------------------------------------------------


def effective_time(alias: str = "") -> str:
    """SQL expression for a run's effective timestamp: ``run_timestamp`` else ``ingested_at``.

    ``alias`` is an optional table qualifier *including the trailing dot* (e.g.
    ``"tr."`` when ``test_runs`` is joined as ``tr``; ``""`` when it is the only
    table). The returned text is a static column expression — it contains no
    caller-supplied values — so callers concatenate it into SQL the same way they
    concatenate ``FAIL_STATUSES_SQL``; every actual value still binds through ``?``.
    """
    return f"COALESCE({alias}run_timestamp, {alias}ingested_at)"
