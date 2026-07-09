"""Validation of the untrusted tool arguments — the input contract.

Distinct from ``domain`` (which validates the *model's* output): this guards
what the agent passes *in* — presence, type, byte size, and the format enum —
and returns the three normalized values the engine needs.
"""

from __future__ import annotations

from typing import Any

from . import schema


def _check_size(field: str, value: str, limit: int) -> None:
    """Raise ValueError if ``value`` is larger than ``limit`` bytes (UTF-8)."""
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"`{field}` exceeds the {limit // 1024} KB limit.")


def validate_input(args: Any) -> tuple[str, str, str]:
    """Return ``(raw_text, context, format)`` or raise ValueError with a clear message."""
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be a JSON object.")

    raw_text = args.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("`raw_text` is required and must be a non-empty string.")
    _check_size("raw_text", raw_text, schema.MAX_RAW_TEXT_BYTES)

    context = args.get("context")
    if context is None:  # absent or JSON null -> treat as empty
        context = ""
    if not isinstance(context, str):
        raise ValueError("`context` must be a string.")
    _check_size("context", context, schema.MAX_CONTEXT_BYTES)

    fmt = args.get("format")
    if fmt is None or fmt == "":  # absent, null, or empty -> default
        fmt = schema.DEFAULT_FORMAT
    if not isinstance(fmt, str):
        raise ValueError("`format` must be a string.")
    if fmt not in schema.ALLOWED_FORMATS:
        allowed = ", ".join(schema.ALLOWED_FORMATS)
        raise ValueError(f"`format` must be one of: {allowed}.")

    return raw_text, context, fmt
