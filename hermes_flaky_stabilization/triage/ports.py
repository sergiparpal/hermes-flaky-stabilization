"""Typed ports for the Hermes capabilities this stage depends on.

The stage follows a ports-and-adapters shape: ``__init__.register`` is the only
adapter that touches Hermes, and it *injects* the host's ``llm`` into the
otherwise pure-stdlib pipeline. That capability used to cross the boundary as a
bare ``Any``; this module names the narrow surface it must satisfy (Interface
Segregation — depend only on the methods actually called) so the contract is
explicit, type-checkable, and easy to fake in tests.

These are :class:`typing.Protocol` definitions: structural, import-only, and
stdlib. They impose no runtime requirement on the real Hermes objects — the host
``ctx.llm`` already satisfies :class:`LlmPort` by shape, no registration needed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StructuredResult(Protocol):
    """The result object returned by :meth:`LlmPort.complete_structured`.

    Mirrors ``agent.plugin_llm.PluginLlmStructuredResult``: ``parsed`` is the
    decoded object when ``content_type == "json"``, else ``text`` carries the
    raw model output.
    """

    parsed: Any
    content_type: str
    text: str


@runtime_checkable
class LlmPort(Protocol):
    """The single host-LLM method the classifier uses.

    The real implementation (``ctx.llm``) exposes more, but the classifier only
    needs a structured completion validated against a JSON schema. Keyword-only
    to match ``agent.plugin_llm.PluginLlm.complete_structured``.
    """

    def complete_structured(
        self,
        *,
        instructions: str,
        input: Sequence[Any],
        json_schema: Any | None = ...,
        schema_name: str | None = ...,
        temperature: float | None = ...,
        max_tokens: int | None = ...,
        timeout: float | None = ...,
        purpose: str | None = ...,
    ) -> StructuredResult: ...
