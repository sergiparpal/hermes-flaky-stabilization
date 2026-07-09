"""Structural typing for the slice of the Hermes host this plugin depends on.

The plugin reaches the host through a tiny surface: ``ctx.llm`` and its
``complete_structured`` method, the ``parsed`` attribute on the result, and (at
load time) ``ctx.register_tool`` / ``ctx.register_command``. Modelling that
surface as ``Protocol``s — instead of threading ``ctx: Any`` through every layer
— documents the verified contract in-repo, gives type checkers and editors the
one boundary that talks to Hermes to check, and keeps the dependency narrow: we
declare the methods we call and nothing else.

The ``complete_structured`` signature mirrors ``agent/plugin_llm.py`` in
``NousResearch/hermes-agent`` (verified against ``main``). The host accepts more
keyword arguments than the plugin passes; only the ones used here are declared.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol


class LlmUnavailable(RuntimeError):
    """Raised when ``ctx.llm`` is absent, so the handler can report it cleanly."""


class StructuredResult(Protocol):
    """The part of ``PluginLlmStructuredResult`` the plugin reads.

    ``parsed`` is the decoded JSON object, or ``None`` when the host could not
    parse the model's output into JSON.
    """

    parsed: Any


class LlmClient(Protocol):
    """The single host method this plugin calls to do its work."""

    def complete_structured(
        self,
        *,
        instructions: str,
        input: Sequence[dict[str, str]],
        json_schema: dict[str, Any],
        schema_name: str,
        purpose: str,
        temperature: float,
        max_tokens: int,
    ) -> StructuredResult: ...


class HostContext(Protocol):
    """The runtime ``ctx`` slice used to serve a request: just ``llm``.

    ``llm`` is ``None`` when no model is configured; the engine checks for that
    and raises :class:`LlmUnavailable`.
    """

    llm: LlmClient | None


class PluginHost(HostContext, Protocol):
    """The load-time ``ctx`` slice: registration plus ``llm`` (from the base).

    ``register_command`` is called defensively at load time (some host versions
    may not expose it); declaring it here documents the arguments the plugin
    passes rather than asserting every host provides it.
    """

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., str],
    ) -> None: ...

    def register_command(
        self,
        *,
        name: str,
        handler: Callable[..., str],
        description: str,
        args_hint: str,
    ) -> None: ...
