"""Export learned recipes as an agentskills.io-style markdown mirror.

``ctx.register_skill`` is read-only and resolved at startup (plan §3.3), so
runtime knowledge lives in SQLite; this exporter refreshes
``learned-patterns.md`` next to the database after every persisted recipe —
a human-readable mirror that is also picked up on the NEXT host startup by
anyone who chooses to register it.

Because that registration makes this file agent-facing, every interpolated
field that originates in a trace.zip or an LLM diagnosis is treated as a
stored prompt-injection vector and sanitized before it is written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .recipes.store import HealerStore

_INLINE_CAP = 240


def _inline(value, cap: int = _INLINE_CAP) -> str:
    """Render an untrusted value as single-line inline code.

    Evidence strings and patch-op fields are trace-/LLM-derived: embedded
    newlines or backtick fences could break out of the surrounding markdown
    and smuggle instructions into an agent-registered skill. Collapse all
    whitespace runs (including newlines), drop backticks, cap the length,
    and wrap in inline code.
    """
    text = re.sub(r"\s+", " ", str(value if value is not None else "?")).strip()
    text = text.replace("`", "'")
    if len(text) > cap:
        text = text[:cap] + "…"
    return f"`{text or '?'}`"


def _fenced(text: str, info: str = "json") -> list[str]:
    """Fence *text* with more backticks than any run it contains.

    A fence of N backticks is only closed by a run of >= N (CommonMark), so
    content can never terminate the block early and inject markdown after it.
    """
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{info}", text, fence]


def export_learned_patterns(store: HealerStore, out_path: Path | str | None = None) -> Path:
    out = Path(out_path) if out_path else store.db_path.parent / "learned-patterns.md"
    recipes = store.all_recipes()

    lines = [
        "---",
        "name: flaky-healer-learned-patterns",
        "description: Auto-generated healing recipes learned by hermes-flaky-healer "
        f"({len(recipes)} signature{'s' if len(recipes) != 1 else ''}); operational "
        "store is state.db, this file is a read-only mirror.",
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
            f"## {_inline(recipe.signature[:8])} — {_inline(diagnosis.get('cause', 'unknown'))}"
            f" → {_inline(recipe.strategy)}",
            "",
            f"- created: {_inline(recipe.created_ts)} · last used: "
            f"{_inline(recipe.last_used_ts or 'never')}",
            f"- stats: {stats}",
            f"- failing action: {_inline(parts.get('failing_action', '?'))} · selector shape: "
            f"{_inline(parts.get('selector_shape', '?'))} · error: "
            f"{_inline(parts.get('error_class', '?'))}",
            f"- evidence: {_inline(diagnosis.get('evidence', ''), cap=400)}",
            "",
            # json.dumps escapes control characters inside strings, so the only
            # fence-breakout vector left is a backtick run — neutralized by the
            # adaptive fence length.
            *_fenced(json.dumps(recipe.patch_ops, indent=2)),
            "",
        ]
    text = "\n".join(lines)
    # The exporter runs after every persisted recipe; skip the O(N) disk write
    # (and the file-mtime churn) when the regenerated mirror is byte-identical.
    try:
        if out.read_text(encoding="utf-8") == text:
            return out
    except (OSError, ValueError):
        pass
    out.write_text(text, encoding="utf-8")
    return out
