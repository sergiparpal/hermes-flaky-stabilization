"""Recipe matching: exact signature → relaxed → miss (plan §4.6)."""

from __future__ import annotations

from .signature import is_degenerate, relaxed_key, signature_hash
from .store import HealerStore, Recipe


def match(store: HealerStore, parts: dict) -> tuple[Recipe | None, str]:
    """Returns (recipe, kind) with kind ∈ {"exact", "relaxed", "miss"}.

    Relaxed matching drops selector_shape/message_shape and keeps
    (framework, error_class, failing_action); the best-performing recipe
    (most successes, fewest failures, oldest first for stability) wins.

    Degenerate parts (no failure information at all — e.g. the trace of a
    passing retry) hash to a constant value under which unrelated failures
    would all collide; they are always a miss.
    """
    if is_degenerate(parts):
        return None, "miss"
    exact = store.get_recipe(signature_hash(parts))
    if exact is not None:
        return exact, "exact"

    candidates = store.recipes_by_relaxed_key(relaxed_key(parts))
    if candidates:
        candidates.sort(key=lambda r: (-r.successes, r.failures, r.created_ts))
        return candidates[0], "relaxed"
    return None, "miss"
