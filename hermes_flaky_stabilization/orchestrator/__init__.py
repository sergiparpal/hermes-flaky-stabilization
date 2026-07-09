"""Orchestration layer: pipeline control flow, dedup, gates, and hooks.

Phase 5 ships the lifecycle hooks (incident context injection + session-start
sync trigger); the pipeline itself (``stabilize_test_failure``), dedup, the
PII gate, and the ``pre_tool_call`` approval escalation land in Phase 6.
"""
