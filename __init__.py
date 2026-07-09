"""Hermes compatibility shim — the plugin's load-time entry point.

Hermes loads a plugin by executing the ``__init__.py`` at the plugin *directory*
root. That directory is ``hermes-flaky-stabilization`` (hyphenated, so not a
valid import name); the real implementation lives in the sibling
``hermes_flaky_stabilization`` package. This file just re-exports its
``register`` entry point.

Hermes loads this file via ``importlib`` (``spec_from_file_location`` with
``submodule_search_locations``) and does *not* put the plugin directory on
``sys.path``, so the absolute import below would not resolve on its own when the
plugin is dropped into ``~/.hermes/plugins/`` without being pip-installed.
Adding this file's own directory to ``sys.path`` makes
``hermes_flaky_stabilization`` importable by its real name in every case —
pip-installed or not — without changing how the package's own modules import
each other (always relative, within the package).
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)

from hermes_flaky_stabilization import register  # noqa: E402  (after sys.path setup)

__all__ = ["register"]
