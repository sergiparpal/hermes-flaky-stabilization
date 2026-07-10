"""Package-wide security source scan (plan Phase 3 task 3, §3.11).

No dynamic execution anywhere, and subprocess/os.system only in the explicitly
pinned allowlist — the modules whose *job* is process execution:

* ``detective/cli.py``          — install-cron's single sanctioned
                                  ``hermes cron create`` subprocess
* ``healer/sandbox/*``          — the sandbox backends run the test process
* ``healer/gitflow.py``         — (via dispatch_tool only; listed defensively
                                  in case the ported module mentions the word)
* ``cli.py``                    — hosts install-cron composition (Phase 7)

Growing this list is a reviewed act: add the module here *with a reason*.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "hermes_flaky_stabilization"

# module path (relative to the package) -> why it may spawn processes
SUBPROCESS_ALLOWLIST = {
    "detective/cli.py": "install-cron's sanctioned `hermes cron create` call",
    "cli.py": "mounts install-cron (same sanctioned subprocess)",
    "healer/flaky_healer/sandbox/subproc.py": "subprocess sandbox backend",
    "healer/flaky_healer/sandbox/docker.py": "docker CLI invocation",
    "healer/flaky_healer/sandbox/base.py": "sandbox probes (docker availability)",
    "healer/flaky_healer/gitflow.py": "local `git apply --check` preflight; "
                                      "pushes/PRs still go through dispatch_tool",
}

# Import-level detection: actually *using* subprocess requires importing it;
# a config key or docstring merely naming the word is not process execution.
_DYNAMIC_EXEC = ("eval(", "exec(", "os.system(", "os.popen(")
_SUBPROCESS = ("import subprocess", "from subprocess")


def _package_sources():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_dynamic_execution_anywhere():
    for path in _package_sources():
        src = path.read_text(encoding="utf-8")
        for token in _DYNAMIC_EXEC:
            assert token not in src, f"{path.relative_to(PACKAGE)} contains {token!r}"


def test_subprocess_only_in_pinned_allowlist():
    for path in _package_sources():
        rel = str(path.relative_to(PACKAGE))
        src = path.read_text(encoding="utf-8")
        uses = any(token in src for token in _SUBPROCESS)
        if uses:
            assert rel in SUBPROCESS_ALLOWLIST, (
                f"{rel} mentions subprocess but is not in the pinned allowlist"
            )


def test_allowlist_entries_exist():
    """A stale allowlist entry hides a real regression — prune it.

    Every entry must exist on disk, no carve-outs: the healer shipped in
    Phase 4, so its entries are held to the same standard as everyone else's.
    """
    for rel in SUBPROCESS_ALLOWLIST:
        assert (PACKAGE / rel).exists(), f"allowlisted module missing: {rel}"
