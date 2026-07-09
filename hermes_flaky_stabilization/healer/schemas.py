"""Tool schemas, OpenAI function-calling style (plan §3.3).

The description lives INSIDE each schema — it is not a register_tool kwarg.
DIAGNOSIS_SCHEMA (used for ctx.llm.complete_structured) is re-exported from
flaky_healer.diagnose, its single source of truth.
"""

try:
    from .flaky_healer.diagnose import DIAGNOSIS_SCHEMA
except ImportError:  # flat import context (tests, path-loaded module)
    from flaky_healer.diagnose import DIAGNOSIS_SCHEMA

__all__ = [
    "ALL_TOOL_SCHEMAS",
    "ANALYZE_PLAYWRIGHT_TRACE",
    "DIAGNOSIS_SCHEMA",
    "FETCH_CI_LOGS",
    "HEAL_FLAKY_TEST",
    "LIST_HEALING_RECIPES",
    "STRATEGY_NAMES",
]

STRATEGY_NAMES = ["bump_timeout", "testid_selector", "await_state"]

FETCH_CI_LOGS = {
    "name": "fetch_ci_logs",
    "description": (
        "Download a failed GitHub Actions run's logs and locally pre-filter them to the "
        "failure-relevant fragment (~15 KB max) before anything reaches an LLM. Returns run "
        "metadata, the failed jobs/steps, and the filtered log. Requires GITHUB_TOKEN."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "build_id": {
                "type": "string",
                "description": "GitHub Actions run id (the numeric id in the run URL).",
            },
            "repo": {
                "type": "string",
                "description": "Repository in 'owner/name' form, e.g. 'acme/webshop'.",
            },
        },
        "required": ["build_id", "repo"],
    },
}

ANALYZE_PLAYWRIGHT_TRACE = {
    "name": "analyze_playwright_trace",
    "description": (
        "Parse a Playwright trace.zip, extract the failing action, selector, error and "
        "timing/network context, and return a structured flakiness diagnosis "
        "(cause, evidence, recommended fix strategy, confidence)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "trace_path": {
                "type": "string",
                "description": "Filesystem path to a Playwright trace.zip file.",
            },
        },
        "required": ["trace_path"],
    },
}

HEAL_FLAKY_TEST = {
    "name": "heal_flaky_test",
    "description": (
        "End-to-end healer for a flaky Playwright test: diagnose (trace/logs/recipes), apply a "
        "fix strategy to a copy of the repo inside an isolated sandbox, validate by burn-in "
        "(reproduce M runs, then require N/N green post-patch), and either return the diff "
        "(mode=suggest) or open a PR through the host's approval pipeline (mode=pr). "
        "Successful heals are persisted as reusable recipes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo_dir": {
                "type": "string",
                "description": (
                    "Path to the local checkout whose Playwright project root contains "
                    "playwright.config.*."
                ),
            },
            "test_id": {
                "type": "string",
                "description": "Spec path relative to repo_dir, e.g. 'tests/login.spec.ts'.",
            },
            "trace_path": {
                "type": "string",
                "description": "Optional path to a failing trace.zip for this test.",
            },
            "build_id": {
                "type": "string",
                "description": "Optional GitHub Actions run id to pull failure logs from.",
            },
            "repo": {
                "type": "string",
                "description": "Repository 'owner/name' (required when build_id is given).",
            },
            "mode": {
                "type": "string",
                "enum": ["suggest", "pr"],
                "description": "suggest (default): return diff + report only. pr: branch, "
                "commit, push and open a PR via the host's approval pipeline.",
            },
            "strategy": {
                "type": "string",
                "enum": STRATEGY_NAMES,
                "description": "Optional override of the automatically selected fix strategy.",
            },
        },
        "required": ["repo_dir", "test_id"],
    },
}

LIST_HEALING_RECIPES = {
    "name": "list_healing_recipes",
    "description": (
        "List learned healing recipes (failure signature → fix procedure) with their "
        "hit/success/failure statistics."
    ),
    "parameters": {"type": "object", "properties": {}},
}

ALL_TOOL_SCHEMAS = {
    "fetch_ci_logs": FETCH_CI_LOGS,
    "analyze_playwright_trace": ANALYZE_PLAYWRIGHT_TRACE,
    "heal_flaky_test": HEAL_FLAKY_TEST,
    "list_healing_recipes": LIST_HEALING_RECIPES,
}
