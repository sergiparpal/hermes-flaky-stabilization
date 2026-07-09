"""System prompt, severity-rubric rendering, and few-shot examples.

Design decision: the model is instructed to ALWAYS return a single JSON object;
the handler renders Markdown afterwards. The JSON shape is also enforced by
``ctx.llm.complete_structured(json_schema=...)``. A single JSON contract keeps
validation deterministic.

The ``FEW_SHOT_EXAMPLES`` outputs double as test fixtures (they conform to
``schema.BUG_REPORT_OUTPUT_SCHEMA``) — see ``tests/``.
"""

from __future__ import annotations

import functools
import json

from . import schema

# Core system prompt, kept under ~400 words.
# {rubric} is substituted (via str.replace) with the rendered severity rubric,
# and {max_title} with the title-length cap.
SYSTEM_PROMPT = """\
You are a meticulous bug-report editor. You turn a raw, often vague or incomplete
bug report into one clean, structured report. Return a single JSON object and
nothing else — no prose, no Markdown, no code fences.

Rules:
1. Never invent facts. If the report does not state the OS, version, environment,
   exact error message, or steps, do NOT guess them. Instead, name each missing
   item in `missing_evidence`.
2. Prefer `severity` = "unknown" over guessing. Only assign a level when the
   report gives a real signal about user impact.
3. Preserve the user's exact wording for verbatim error messages, stack traces,
   and quoted strings. Reorganize and clarify everything else, but never
   paraphrase an error message.
4. One bug per report. If the input mixes multiple unrelated bugs, structure the
   FIRST one and add a `missing_evidence` item stating that additional bug(s)
   were detected and should be filed separately.

Field guidance:
- `title`: at most {max_title} characters, specific.
- `summary`: 1-2 sentences.
- `reproduction_steps`: an ordered array; use [] if the report gives none (and
  add a `missing_evidence` entry).
- `expected_behavior` / `actual_behavior`: use "" if absent (and note in
  `missing_evidence`).
- `severity`: exactly one of critical, high, medium, low, unknown.
- `severity_rationale`: 1-2 sentences justifying the level against the rubric.
- `missing_evidence`: list every useful fact the report lacks.

Severity rubric (fixed; choose strictly from these):
{rubric}
"""


def _render_rubric() -> str:
    return "\n".join(
        f"- {level}: {desc}" for level, desc in schema.SEVERITY_RUBRIC.items()
    )


# Three few-shot examples. Outputs conform to the output schema.
FEW_SHOT_EXAMPLES: list[dict] = [
    # Example A — vague one-liner -> many missing_evidence, severity unknown.
    {
        "input": "login broken sometimes",
        "output": {
            "title": "Login fails intermittently",
            "summary": (
                "A user reports that login is broken some of the time. No "
                "reproduction details, environment, or error text were provided."
            ),
            "reproduction_steps": [],
            "expected_behavior": "",
            "actual_behavior": "Login sometimes fails.",
            "severity": "unknown",
            "severity_rationale": (
                "The report gives no information about scope, frequency, or "
                "impact, so a severity level cannot be assigned."
            ),
            "missing_evidence": [
                "Exact steps to reproduce",
                "How often it fails (always, intermittently, specific accounts?)",
                "Error message or screenshot shown on failure",
                "Affected platform/OS/browser and application version",
                "What the user expected to happen",
                "Whether other users are affected",
            ],
        },
    },
    # Example B — detailed report -> severity high, empty missing_evidence.
    {
        "input": (
            "Checkout 'Pay now' button does nothing on Safari 17 (macOS 14.4), "
            "app v3.2.1. Steps: 1) add item to cart 2) go to checkout 3) click "
            "'Pay now' - nothing happens and no network request fires. Console "
            "shows 'TypeError: undefined is not a function' at checkout.js:88. "
            "Expected: payment is processed. Happens 100% of the time on Safari; "
            "Chrome works fine. This blocks all purchases for Safari users."
        ),
        "output": {
            "title": "Checkout 'Pay now' button is unresponsive on Safari 17",
            "summary": (
                "Clicking 'Pay now' on Safari 17 / macOS 14.4 (app v3.2.1) does "
                "nothing and fires no network request, blocking checkout. Chrome "
                "is unaffected."
            ),
            "reproduction_steps": [
                "Add an item to the cart",
                "Go to checkout",
                "Click 'Pay now'",
            ],
            "expected_behavior": "Payment is processed and the order completes.",
            "actual_behavior": (
                "Nothing happens and no network request fires. Console shows: "
                "TypeError: undefined is not a function (checkout.js:88)."
            ),
            "severity": "high",
            "severity_rationale": (
                "A major workflow (payment) is fully blocked for all Safari users "
                "with no workaround; Chrome still works, so it is not a total "
                "outage."
            ),
            "missing_evidence": [],
        },
    },
    # Example C — two unrelated bugs -> structure the first, flag the second.
    {
        "input": (
            "Two things: the profile page avatar looks stretched/squished on "
            "mobile. Also, exporting a report to CSV crashes the app with "
            "'OutOfMemoryError' on large datasets (>100k rows)."
        ),
        "output": {
            "title": "Profile avatar is distorted on mobile",
            "summary": (
                "On mobile, the profile-page avatar appears stretched or "
                "squished. A second, unrelated bug about CSV export crashing was "
                "also reported and should be filed separately."
            ),
            "reproduction_steps": [
                "Open the profile page on a mobile device",
                "Observe the avatar image",
            ],
            "expected_behavior": "The avatar displays with the correct aspect ratio.",
            "actual_behavior": "The avatar appears stretched or squished.",
            "severity": "low",
            "severity_rationale": (
                "A cosmetic display issue on the profile page with no functional "
                "impact."
            ),
            "missing_evidence": [
                "Specific mobile device, OS, and application version",
                "Screenshot of the distorted avatar",
                "Whether it affects all images or only certain aspect ratios",
                (
                    "A second, unrelated bug was detected (CSV export crashes with "
                    "OutOfMemoryError on datasets larger than 100k rows); file it "
                    "as a separate report"
                ),
            ],
        },
    },
]


@functools.lru_cache(maxsize=1)
def build_instructions() -> str:
    """Assemble the full ``instructions`` string for ``complete_structured``.

    Deterministic (no inputs), so the result is cached and built once.
    """
    system = SYSTEM_PROMPT.replace("{rubric}", _render_rubric()).replace(
        "{max_title}", str(schema.MAX_TITLE_CHARS)
    )
    parts = [system, "Worked examples:"]
    for i, example in enumerate(FEW_SHOT_EXAMPLES, 1):
        parts.append(
            f"\nExample {i}\nInput:\n{example['input']}\n"
            f"Output:\n{json.dumps(example['output'], ensure_ascii=False)}"
        )
    parts.append("\nNow produce the JSON object for the user's report below.")
    return "\n".join(parts)
