"""Export learned recipes as an agentskills.io-style markdown mirror.

``ctx.register_skill`` is read-only and resolved at startup (plan §3.3), so
runtime knowledge lives in SQLite; this exporter refreshes
``learned-patterns.md`` next to the database after every persisted recipe —
a human-readable mirror that is also picked up on the NEXT host startup by
anyone who chooses to register it.
"""

from __future__ import annotations

import json
from pathlib import Path

from .recipes.store import HealerStore


def export_learned_patterns(store: HealerStore, out_path: Path | str | None = None) -> Path:
    out = Path(out_path) if out_path else store.db_path.parent / "learned-patterns.md"
    recipes = store.all_recipes()

    lines = [
        "---",
        "name: flaky-healer-learned-patterns",
        "description: Auto-generated healing recipes learned by hermes-flaky-healer "
        f"({len(recipes)} signature{'s' if len(recipes) != 1 else ''}); operational "
        "store is healer.db, this file is a read-only mirror.",
        "---",
        "",
        "# Learned healing patterns",
        "",
        "A recipe maps a normalized failure signature to a fix procedure. Matching a",
        "recipe skips re-diagnosis but NEVER skips the full reproduce/burn-in cycle.",
        "",
    ]
    for recipe in recipes:
        diagnosis = recipe.diagnosis or {}
        parts = recipe.signature_parts or {}
        stats = (
            f"hits {recipe.hits} · successes {recipe.successes} · failures {recipe.failures}"
        )
        lines += [
            f"## `{recipe.signature[:8]}` — {diagnosis.get('cause', 'unknown')} → "
            f"{recipe.strategy}",
            "",
            f"- created: {recipe.created_ts} · last used: {recipe.last_used_ts or 'never'}",
            f"- stats: {stats}",
            f"- failing action: `{parts.get('failing_action', '?')}` · selector shape: "
            f"`{parts.get('selector_shape', '?')}` · error: `{parts.get('error_class', '?')}`",
            f"- evidence: {diagnosis.get('evidence', '')}",
            "",
            "```json",
            json.dumps(recipe.patch_ops, indent=2),
            "```",
            "",
        ]
    text = "\n".join(lines)
    # The exporter runs after every persisted recipe; skip the O(N) disk write
    # (and the file-mtime churn) when the regenerated mirror is byte-identical.
    try:
        if out.read_text() == text:
            return out
    except OSError:
        pass
    out.write_text(text)
    return out
