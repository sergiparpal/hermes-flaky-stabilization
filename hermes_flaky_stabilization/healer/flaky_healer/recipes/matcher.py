"""Recipe matching: exact signature → relaxed → miss (plan §4.6)."""

from __future__ import annotations

from .signature import relaxed_key, signature_hash
from .store import HealerStore, Recipe


def match(store: HealerStore, parts: dict) -> tuple[Recipe | None, str]:
    """Returns (recipe, kind) with kind ∈ {"exact", "relaxed", "miss"}.

    Relaxed matching drops selector_shape/message_shape and keeps
    (framework, error_class, failing_action); the best-performing recipe
    (most successes, fewest failures, oldest first for stability) wins.
    """
    exact = store.get_recipe(signature_hash(parts))
    if exact is not None:
        return exact, "exact"

    candidates = store.recipes_by_relaxed_key(relaxed_key(parts))
    if candidates:
        candidates.sort(key=lambda r: (-r.successes, r.failures, r.created_ts))
        return candidates[0], "relaxed"
    return None, "miss"
