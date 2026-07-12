"""The canonical bug-report domain object and its one validated constructor.

``ImprovedBugReport`` is the single typed representation that flows from the
engine through to rendering; only the JSON output edge serializes it back to a
dict (``to_dict``). It owns its own normalization: by construction a report's
``title`` is always a single line of at most ``schema.MAX_TITLE_CHARS``
characters, so both output formats inherit that invariant and no rendering step
has to re-enforce it. ``from_parsed`` is the only way to build one from
untrusted model JSON — it validates the contract before constructing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import schema


@dataclass
class ImprovedBugReport:
    """Canonical output structure, independent of the chosen output format."""

    title: str
    summary: str
    reproduction_steps: list[str]
    expected_behavior: str
    actual_behavior: str
    severity: str
    severity_rationale: str
    missing_evidence: list[str]

    def __post_init__(self) -> None:
        # The title invariant is enforced on every construction so both the
        # Markdown and the json paths inherit a single-line, length-capped
        # title — the json path skips the Markdown sanitizer, so this is what
        # keeps its title clean, and it makes Markdown truncation count visible
        # characters rather than a stray newline. Idempotent: re-normalizing an
        # already-clean title is a no-op.
        title = " ".join(self.title.split())
        if len(title) > schema.MAX_TITLE_CHARS:
            title = title[: schema.MAX_TITLE_CHARS - 1].rstrip() + "…"
        self.title = title
        for field_name in (
            "summary", "expected_behavior", "actual_behavior", "severity_rationale"
        ):
            value = getattr(self, field_name)
            setattr(self, field_name, value[:schema.MAX_FIELD_CHARS])
        for field_name in ("reproduction_steps", "missing_evidence"):
            values = getattr(self, field_name)[:schema.MAX_LIST_ITEMS]
            setattr(self, field_name, [
                value[:schema.MAX_LIST_ITEM_CHARS] for value in values
            ])

    @classmethod
    def from_parsed(cls, data: Any) -> ImprovedBugReport:
        """Validate and normalize parsed model JSON into a report.

        Raises ``ValueError`` if ``data`` is not a JSON object, is missing a
        required field, carries an out-of-rubric severity, or types either list
        field as a non-array. Scalar fields are coerced to ``str`` and the list
        fields to ``list[str]``; the title invariant is applied in
        ``__post_init__``.
        """
        if not isinstance(data, dict):
            raise ValueError("model output was not a JSON object")

        missing = [f for f in schema.REQUIRED_OUTPUT_FIELDS if f not in data]
        if missing:
            raise ValueError(f"model output missing fields: {', '.join(missing)}")

        severity = data.get("severity")
        if severity not in schema.SEVERITY_LEVELS:
            allowed = ", ".join(schema.SEVERITY_LEVELS)
            raise ValueError(f"invalid severity {severity!r}; must be one of: {allowed}")

        for arr in ("reproduction_steps", "missing_evidence"):
            if not isinstance(data.get(arr), list):
                raise ValueError(f"`{arr}` must be an array")

        return cls(
            title=str(data["title"]),
            summary=str(data["summary"]),
            reproduction_steps=[str(s) for s in data["reproduction_steps"]],
            expected_behavior=str(data["expected_behavior"]),
            actual_behavior=str(data["actual_behavior"]),
            severity=str(severity),
            severity_rationale=str(data["severity_rationale"]),
            missing_evidence=[str(s) for s in data["missing_evidence"]],
        )

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict view, for JSON serialization at the output edge."""
        return asdict(self)
