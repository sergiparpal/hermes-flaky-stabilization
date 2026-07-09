"""hermes-flaky-stabilization — the unified flaky-test stabilization pipeline.

One ``kind: standalone`` Hermes plugin absorbing seven stages: test-history
indexing, flaky detection, CI triage, sandboxed healing, bug-report
structuring, PII masking validation, and a local Jira incident index — plus
the orchestration layer that connects them.

``register(ctx)`` is the only Hermes-facing entry point. Heavy submodule
imports are deferred into it so that merely importing this package at Hermes
startup stays cheap and side-effect-free.

NOTE (kind auto-detection guard, plan §3.2): the loader scans the first 8 KiB
of the plugin root's ``__init__.py`` for memory-provider marker strings and
coerces the plugin kind when it finds them. Neither this file, the root shim,
nor any docstring in the entry path may mention those identifiers.
"""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:
    """Entry point Hermes calls once at plugin load time."""
    from . import registration

    registration.register(ctx)


__all__ = ["register", "__version__"]
