"""Bootstrap for the ported PII suites (masking-validator + redaction).

The masking-validator tests import ``detectors`` / ``scanner`` as top-level
modules (that repo's conftest put its root on ``sys.path``); the redaction
tests import ``jira_incidents.redaction``. Alias both spellings onto the
unified ``pii`` stage so the ported test files run unchanged.
"""

import importlib
import sys
from pathlib import Path

import pytest

_pii = importlib.import_module("hermes_flaky_stabilization.pii")
_detectors = importlib.import_module("hermes_flaky_stabilization.pii.detectors")
_scanner = importlib.import_module("hermes_flaky_stabilization.pii.scanner")
_redaction = importlib.import_module("hermes_flaky_stabilization.pii.redaction")

sys.modules.setdefault("detectors", _detectors)
sys.modules.setdefault("scanner", _scanner)

# The redaction tests import ``jira_incidents.redaction`` (the module's legacy
# home). Map the alias onto the REAL incidents stage — the same mapping
# tests/incidents/conftest.py installs — so the two suites agree no matter
# which loads first.
sys.modules.setdefault(
    "jira_incidents", importlib.import_module("hermes_flaky_stabilization.incidents")
)
sys.modules.setdefault("jira_incidents.redaction", _redaction)


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME (and Path.home) into a throwaway tmp directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
