"""Isolated sandboxes for reproducing and burn-in-validating flaky tests.

Docker (--network=none) is the default; an env-scrubbed subprocess backend is
the documented weaker fallback. Every result is tagged with its isolation mode.
"""
