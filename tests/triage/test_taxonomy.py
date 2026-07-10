"""taxonomy: single-source-of-truth integrity + drift guards.

``taxonomy.CATEGORIES`` is the one place a category is defined. Most other
representations are *derived* (the schema enum, the prose block, the heuristic
rules) so they cannot drift. Two representations cannot be derived — the tool
schema ``description`` and the README table are human-facing prose — so this
module guards them: if a category is added/renamed in ``taxonomy.py`` without
updating those, the suite fails instead of shipping an inconsistent contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hermes_plugins.hermes_ci_triage import classifier, taxonomy

PLUGIN_DIR = Path(__file__).resolve().parents[2]  # unified repo root


# --------------------------------------------------------------------------
# Internal consistency of the single source of truth
# --------------------------------------------------------------------------

def test_categories_are_unique():
    keys = [c.key for c in taxonomy.CATEGORIES]
    assert len(keys) == len(set(keys)), "duplicate category key"
    priorities = [c.priority for c in taxonomy.CATEGORIES]
    assert len(priorities) == len(set(priorities)), "duplicate heuristic priority"


def test_default_category_is_in_taxonomy():
    assert taxonomy.DEFAULT_CATEGORY in taxonomy.TAXONOMY


def test_classifier_reexports_match_source():
    assert classifier.TAXONOMY == taxonomy.TAXONOMY
    enum = classifier.CLASSIFICATION_SCHEMA["properties"]["category"]["enum"]
    assert set(enum) == set(taxonomy.TAXONOMY)
    assert len(enum) == len(taxonomy.TAXONOMY)


def test_heuristic_rules_derive_from_taxonomy():
    assert classifier._HEURISTIC_RULES is taxonomy.HEURISTIC_RULES
    rule_keys = [key for key, _ in taxonomy.HEURISTIC_RULES]
    # Every rule names a real category, and rules are in ascending priority.
    assert set(rule_keys) <= set(taxonomy.TAXONOMY)
    priorities = [
        c.priority for key in rule_keys for c in taxonomy.CATEGORIES if c.key == key
    ]
    assert priorities == sorted(priorities), "heuristic rules not in priority order"


def test_definitions_block_mentions_every_category():
    block = taxonomy.definitions_block()
    for c in taxonomy.CATEGORIES:
        assert f"- {c.key}:" in block


# --------------------------------------------------------------------------
# Drift guards for the two non-derivable, human-facing representations
# --------------------------------------------------------------------------

def test_tool_description_mentions_every_category():
    """The tool-schema description hand-lists the taxonomy; keep it complete."""
    import hermes_plugins.hermes_ci_triage as plugin

    description = plugin.TRIAGE_SCHEMA["description"]
    for c in taxonomy.CATEGORIES:
        assert c.key in description, (
            f"category '{c.key}' missing from the tool description in __init__.py"
        )


def test_readme_table_mentions_every_category():
    """The README taxonomy table must list every category."""
    readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    for c in taxonomy.CATEGORIES:
        assert f"`{c.key}`" in readme, (
            f"category '{c.key}' missing from README.md"
        )


# --------------------------------------------------------------------------
# Heuristic precision regressions (flaky is the FIRST rule tried, so a loose
# match there misroutes to the healer and gets learned into the pattern store)
# --------------------------------------------------------------------------

def test_flake8_lint_log_is_not_flaky():
    """Regression: `\\bflak` matched "flake8", routing lint failures to flaky."""
    log = (
        "run flake8 on src/\n"
        "src/app.py:12:80: E501 line too long\n"
        "flake8 found 3 problems\n"
        "ERROR: lint step failed\n"
    )
    result = classifier.heuristic_classify(log)
    assert result["category"] != "flaky"


def test_bare_retry_mention_is_not_flaky():
    """Regression: bare `retr(y|ied)` matched infra noise like "will retry in 5s"."""
    log = (
        "connection refused by registry.example\n"
        "will retry in 5s\n"
        "retried 3 times, giving up\n"
    )
    result = classifier.heuristic_classify(log)
    assert result["category"] == "infra"


@pytest.mark.parametrize(
    "line",
    [
        "test_checkout failed but passed on retry",
        "marked flaky by the test framework",
        "known flake in test_login, see #123",
        "flakiness detected in suite payments",
        "test succeeded after 3 retries",
        "test passed after 2 retries",
        "race condition between writer threads",
        "intermittent failure in DNS resolution test",
    ],
)
def test_genuine_flaky_signals_still_match(line):
    result = classifier.heuristic_classify(line)
    assert result["category"] == "flaky", line


def test_missing_fixture_file_is_not_environment():
    """Regression: the dead optional group `(?:bin|exe)?` made ANY missing file
    (including test fixtures — the data category's own definition) classify as
    environment."""
    log = (
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'tests/fixtures/users.json'\n"
        "fixture load failed\n"
    )
    result = classifier.heuristic_classify(log)
    assert result["category"] == "data"


@pytest.mark.parametrize(
    "line",
    [
        "/usr/bin/env: 'python3': No such file or directory",
        "exec /app/tool.exe: no such file or directory",
        "sh: line 1: terraform: command not found",
    ],
)
def test_missing_binary_is_environment(line):
    result = classifier.heuristic_classify(line)
    assert result["category"] == "environment", line
