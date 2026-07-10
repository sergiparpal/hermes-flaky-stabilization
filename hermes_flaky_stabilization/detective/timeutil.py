"""Timestamp normalization for the reader, detection, and CLI layers.

Re-export shim over :mod:`hermes_flaky_stabilization.common.timeutil`, the
single owner of the naive-UTC ISO-8601 rule (see that module for why the
canonical ``T``-separated form makes a string sort a chronological sort).
History and detective used to carry divergent copies; both now delegate here.
Stdlib only — importable by the pure ``detect`` core without pulling in any I/O.
"""

from __future__ import annotations

from ..common.timeutil import iso_to_naive_utc, normalize_ts, window_cutoff

__all__ = ["iso_to_naive_utc", "normalize_ts", "window_cutoff"]
