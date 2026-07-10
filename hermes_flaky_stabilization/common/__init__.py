"""Shared cross-cutting kernel for the unified plugin.

Modules here own the concerns that every stage needs and that used to be
copied per absorbed plugin (timestamp normalization, secret/credential
scrubbing, guarded HTTP, the private SQLite connection recipe). The kernel is
the bottom of the dependency stack: it depends on nothing above it (no stage,
no orchestrator, no Hermes host) so any stage may import it without creating a
cross-stage or upward edge.
"""

from __future__ import annotations
