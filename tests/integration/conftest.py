"""Real-Hermes-core integration harness (plan Phase 8).

Gated on a hermes-agent checkout (``HERMES_REPO`` env or known sibling paths —
the jira-incidents pattern); the whole directory skips cleanly when absent.
Helpers live in ``intg_helpers`` (a uniquely named module — several ported
suites already claim the bare ``conftest`` import name).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# APPEND, never insert(0): the healer suite resolves its own helpers via a
# bare `import conftest`, so this directory must not shadow that name. Only
# the uniquely-named `intg_helpers` needs to resolve from here.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.append(_TESTS_DIR)

from _doubles import find_hermes_repo  # noqa: E402

HERMES_REPO = find_hermes_repo()

if HERMES_REPO is None:
    collect_ignore_glob = ["test_*.py"]
    print(
        "\n[hermes-flaky-stabilization integration] hermes-agent checkout not "
        "found. Set HERMES_REPO to run the integration suite. Skipping.\n",
        file=sys.stderr,
    )


@pytest.fixture
def hermes_home(tmp_path):
    # NOT "hermes-home" — the root autouse env fixture already creates that.
    home = tmp_path / "intg-home"
    home.mkdir()
    return home
