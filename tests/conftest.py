"""Root test bootstrap for hermes-flaky-stabilization.

Responsibilities:
  1. Make the package and the shared doubles importable regardless of pytest's
     ``--import-mode=importlib`` (repo root + tests dir on ``sys.path``).
  2. Provide the harness fixtures every suite shares: ``fake_ctx``,
     ``profile_env`` (throwaway ``HERMES_HOME``), and an autouse guard that
     keeps any test from touching the real ``~/.hermes`` or live credentials.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
for entry in (str(REPO_ROOT), str(TESTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from _doubles import (  # noqa: E402
    FakePluginContext,
    find_hermes_repo,
)

_CRED_VARS = (
    "GITHUB_TOKEN",
    "JIRA_API_TOKEN",
    "JIRA_EMAIL",
    "JIRA_BASE_URL",
)
_PLUGIN_ENV_PREFIXES = ("FLAKY_HEALER_", "HERMES_CI_TRIAGE_", "HERMES_JIRA_")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Deterministic env for every test: no live credentials, no plugin env
    leakage from the host shell, and a throwaway ``HERMES_HOME`` so nothing
    ever reads or writes the real profile."""
    for var in _CRED_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in list(os.environ):
        if var.startswith(_PLUGIN_ENV_PREFIXES):
            monkeypatch.delenv(var, raising=False)
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _reset_history_singletons()
    yield home
    _reset_history_singletons()


def _reset_history_singletons():
    """The history stage keeps a module-level connection/config cache; a stale
    one would leak a previous test's tmp HERMES_HOME into the next test (e.g.
    via triage enrichment, which opens history lazily)."""
    try:
        from hermes_flaky_stabilization.history import storage

        storage.reset_for_tests()
    except Exception:
        pass


@pytest.fixture
def profile_env(_isolated_env, monkeypatch, tmp_path):
    """A throwaway profile home; ``Path.home`` also repointed so even the
    ``~/.hermes`` fallback stays inside the sandbox."""
    fake_home = tmp_path / "user-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return _isolated_env


@pytest.fixture
def fake_ctx(tmp_path):
    return FakePluginContext(hermes_home=tmp_path / "hermes-home")


@pytest.fixture
def hermes_repo():
    repo = find_hermes_repo()
    if repo is None:
        pytest.skip("hermes-agent checkout not found (set HERMES_REPO to run)")
    return repo
