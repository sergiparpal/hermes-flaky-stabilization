"""Pure detection core: classify each test as flaky / consistently_failing / stable.

No I/O, no SQL, no network — this is the testable heart of the plugin. It takes
already-windowed, already-normalized rows (the reader in ``query.py`` does the
SQLite read, the reingest dedup, the ``skipped`` exclusion, and the timestamp
normalization) and returns one :class:`Verdict` per test. Keeping it pure mirrors
the separation the sibling test-history plugin uses (pure logic vs I/O) and lets
the whole classification be unit-tested with plain tuples and zero database.

Only two dependencies, both side-effect-free: the stdlib ``dataclasses`` and this
plugin's import-free ``domain`` constants (status names, the verdict vocabulary,
the failure-status rule, and the test-identity helper) — so the classification
rules have a single owner and cannot drift from the tool/CLI layers. There is no
database, file, or network I/O here by construction (Phase 2 acceptance).

Timestamps: callers pass each row's effective timestamp as a canonical naive-UTC
ISO-8601 *seconds* string (``timeutil.normalize_ts`` produces this), so a plain
string comparison is also a chronological one and the core needs no datetime
parsing.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import domain


@dataclass
class Verdict:
    """One test's classification over the detection window.

    Field set mirrors the ``flaky_verdicts`` table columns (see ``schema.py``) so
    storage can persist a verdict without an impedance-mismatch translation.
    """

    test_key: str
    classname: str | None
    name: str
    file_path: str | None
    passes: int
    fails: int
    runs: int
    window_days: int
    first_seen: str | None
    last_seen: str | None
    last_failure: str | None
    status: str


def compute_verdicts(rows, now, window_days, min_fails, include_errors) -> list[Verdict]:
    """Classify each test from pre-windowed, pre-normalized ``rows``.

    Parameters
    ----------
    rows:
        Iterable of ``(classname, name, file_path, status, eff_ts)`` tuples. The
        caller is responsible for windowing and for excluding ``skipped`` cases;
        this function trusts its input and never reads a clock or a database —
        whatever rows it is given are counted (Phase 2 acceptance: the window
        filter is asserted in the ``query`` tests, not here).
    now:
        The "as-of" time of this scan. Accepted to keep the call's reference time
        explicit and the signature stable; windowing is the reader's job, so the
        classification itself does not consult it.
    window_days, min_fails, include_errors:
        The tunables. ``include_errors`` decides whether ``error`` joins
        ``failed`` in the failure tally (``domain.fail_statuses``).

    Classification (per test, within the window):
      * ``flaky``                ⟺ ``fails >= min_fails`` **and** ``passes >= 1``
      * ``consistently_failing`` ⟺ ``fails >= min_fails`` **and** ``passes == 0``
      * ``stable``               ⟺ otherwise (``fails < min_fails``)

    ``runs == passes + fails``. A ``skipped`` case (should already be excluded by
    the reader) and an ``error`` that is *not* counted as a failure are both
    ignored entirely — neither a pass nor a fail — so they never inflate ``runs``
    and never move ``first_seen``/``last_seen``.
    """
    del now  # see docstring: windowing is the reader's responsibility
    fail_set = set(domain.fail_statuses(include_errors))

    # Accumulate per test, preserving first-seen order for deterministic output.
    acc: dict[str, dict] = {}
    order: list[str] = []

    for classname, name, file_path, status, eff_ts in rows:
        # Normalize identity up front so the stored classname/name columns match the
        # test_key, which make_test_key builds from *stripped* values. Otherwise a
        # name with surrounding whitespace would key as "cls::name" yet store the raw
        # " name ", and the is_flaky bare-name / file_path::name lookups (which match
        # the name column) would miss it. None is preserved (jest omits classname).
        classname = classname.strip() if isinstance(classname, str) else classname
        name = name.strip() if isinstance(name, str) else name
        key = domain.make_test_key(classname, name)
        agg = acc.get(key)
        if agg is None:
            agg = {
                "classname": classname,
                "name": name,
                "file_path": file_path,
                "passes": 0,
                "fails": 0,
                # Running min/max instead of full timestamp lists: the verdict only
                # needs the earliest/latest counted run and the latest failure, so we
                # fold them in as we go. eff_ts is canonical (sortable as a string),
                # so a lexical compare is also a chronological one. This keeps peak
                # memory at O(distinct tests), not O(total counted runs).
                "first": None,       # earliest eff_ts of a counted run (pass or fail)
                "last": None,        # latest eff_ts of a counted run
                "last_fail": None,   # latest eff_ts of a counted failure
            }
            acc[key] = agg
            order.append(key)
        elif not agg["file_path"] and file_path:
            # Some emitters (e.g. jest) omit file_path on some rows; keep the
            # first non-null one we see so reporting can still point at a file.
            agg["file_path"] = file_path

        counted = False
        is_fail = False
        if status == domain.STATUS_PASSED:
            agg["passes"] += 1
            counted = True
        elif status in fail_set:
            agg["fails"] += 1
            counted = True
            is_fail = True
        # else: skipped / uncounted-error / unknown -> ignored (not a run)

        if counted and eff_ts:
            if agg["first"] is None or eff_ts < agg["first"]:
                agg["first"] = eff_ts
            if agg["last"] is None or eff_ts > agg["last"]:
                agg["last"] = eff_ts
            if is_fail and (agg["last_fail"] is None or eff_ts > agg["last_fail"]):
                agg["last_fail"] = eff_ts

    verdicts: list[Verdict] = []
    for key in order:
        agg = acc[key]
        passes, fails = agg["passes"], agg["fails"]
        runs = passes + fails
        if fails >= min_fails and passes >= 1:
            status = domain.VERDICT_FLAKY
        elif fails >= min_fails:  # passes == 0 here
            status = domain.VERDICT_CONSISTENTLY_FAILING
        else:
            status = domain.VERDICT_STABLE

        verdicts.append(
            Verdict(
                test_key=key,
                classname=agg["classname"],
                name=agg["name"],
                file_path=agg["file_path"],
                passes=passes,
                fails=fails,
                runs=runs,
                window_days=window_days,
                first_seen=agg["first"],
                last_seen=agg["last"],
                last_failure=agg["last_fail"],
                status=status,
            )
        )
    return verdicts
