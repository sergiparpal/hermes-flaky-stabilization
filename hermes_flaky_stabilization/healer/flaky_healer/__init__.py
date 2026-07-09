"""Core logic for the hermes-flaky-healer plugin.

Modules in this package use only the Python standard library at runtime and
never import Hermes — the host is reached exclusively through objects injected
by the top-level handlers (ctx, transport, sandbox, LLM callable).
"""
