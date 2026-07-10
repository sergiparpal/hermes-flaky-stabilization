"""The strategy registry and applicable-strategy selection.

Split out of ``base.py``: registration/lookup/ordering is a distinct concern
from the Strategy protocol it stores. Depends on the ``Strategy`` type only for
annotations (``TYPE_CHECKING``), so it never imports ``base`` at runtime — the
selection here is duck-typed on ``.applies()``/``.name``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only — no runtime import, so no base ↔ registry cycle
    from .base import Strategy

REGISTRY: dict[str, Strategy] = {}


def register_strategy(strategy: Strategy) -> Strategy:
    REGISTRY[strategy.name] = strategy
    return strategy


def get_strategy(name: str | None) -> Strategy | None:
    return REGISTRY.get(name or "")


def candidate_strategies(diagnosis: dict, trace) -> list[Strategy]:
    """Applicable strategies, recommended one first (plan §4.3 selection)."""
    ordered: list[Strategy] = []
    recommended = get_strategy((diagnosis or {}).get("recommended_strategy"))
    if recommended is not None and recommended.applies(diagnosis, trace):
        ordered.append(recommended)
    for strategy in REGISTRY.values():
        if strategy in ordered:
            continue
        if strategy.applies(diagnosis, trace):
            ordered.append(strategy)
    return ordered
