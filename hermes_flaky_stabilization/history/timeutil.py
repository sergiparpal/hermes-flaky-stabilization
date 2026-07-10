"""Timestamp normalization for the ingest and query/CLI layers.

Re-export shim over :mod:`hermes_flaky_stabilization.common.timeutil`, the
single owner of the naive-UTC ISO-8601 rule. History and detective used to
carry divergent copies (history's parser rejected the space-separated form
detective accepted); both now delegate here so they cannot drift again. The
stage-local import surface (``from .timeutil import parse_iso_window``) is kept
for compatibility with existing call sites and the legacy import alias.
"""

from __future__ import annotations

from ..common.timeutil import iso_to_naive_utc, parse_iso_window

__all__ = ["iso_to_naive_utc", "parse_iso_window"]
