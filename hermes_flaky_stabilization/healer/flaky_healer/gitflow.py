"""Git/PR write path (plan §4.7).

Every write step (branch, apply, commit, push, PR) is expressed as a
``ctx.dispatch_tool`` invocation so the host's approval/redaction/budget
pipeline governs it — this module NEVER pushes via raw subprocess on the
plugin's behalf. ``execute_locally`` is the pure-local path used by tests
(and only by tests): it executes the same dispatch list against a local
clone + ``file://`` bare remote and simulates the PR-creation tool.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from . import config

PROTECTED_BRANCHES = {"main", "master", "develop", "trunk", "release"}
_BRANCH_PREFIX = "fix/flaky-"


class GitFlowError(RuntimeError):
    pass


def slugify_test_id(test_id: str) -> str:
    stem = Path(test_id).name
    for suffix in (".spec.ts", ".spec.js", ".test.ts", ".test.js", ".ts", ".js"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "test"


def branch_name(test_id: str, signature: str) -> str:
    return f"{_BRANCH_PREFIX}{slugify_test_id(test_id)}-{signature[:8]}"


def build_dispatches(
    *,
    repo_dir: str | Path,
    branch: str,
    base: str | None = None,
    patch_path: str | Path,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    remote: str = "origin",
    git_tool: str | None = None,
    pr_tool: str | None = None,
) -> list[dict]:
    """Ordered dispatch sequence: branch → apply → commit → push → PR.

    Guards (plan §4.7): the branch must carry the fix/flaky- prefix, never
    equal the base, and never be a protected default branch — direct pushes
    to a default branch are never emitted.
    """
    base = base or config.base_branch()
    git_tool = git_tool or config.git_tool()
    pr_tool = pr_tool or config.pr_tool()

    if not branch.startswith(_BRANCH_PREFIX):
        raise GitFlowError(f"refusing branch {branch!r}: must start with {_BRANCH_PREFIX!r}")
    if branch == base or branch in PROTECTED_BRANCHES:
        raise GitFlowError(f"refusing to operate on protected/base branch {branch!r}")

    repo = str(Path(repo_dir).resolve())
    q = shlex.quote

    def git(cmd: str) -> dict:
        return {"tool": git_tool, "arguments": {"command": f"git -C {q(repo)} {cmd}"}}

    return [
        # Cut the branch from base explicitly so the PR isn't accidentally based on
        # whatever the user happened to have checked out.
        git(f"checkout -b {q(branch)} {q(base)}"),
        git(f"apply --index {q(str(Path(patch_path).resolve()))}"),
        git(f"commit -m {q(commit_message)}"),
        git(f"push -u {q(remote)} {q(branch)}"),
        {
            "tool": pr_tool,
            "arguments": {
                "title": pr_title,
                "body": pr_body,
                "head": branch,
                "base": base,
            },
        },
    ]


def checkout_dispatch(repo_dir: str | Path, ref: str, git_tool: str | None = None) -> dict:
    """A single forced-checkout dispatch, used to restore the repo on failure."""
    git_tool = git_tool or config.git_tool()
    repo = str(Path(repo_dir).resolve())
    return {
        "tool": git_tool,
        "arguments": {"command": f"git -C {shlex.quote(repo)} checkout -f {shlex.quote(ref)}"},
    }


def execute_via_ctx(ctx, dispatches: list[dict], *, on_error: dict | None = None) -> list:
    """Run the sequence through the host approval pipeline; stop on failure.

    ``on_error`` (e.g. a ``checkout_dispatch`` back to base) is best-effort
    dispatched if a step fails, so a half-applied sequence doesn't strand the
    user on the fix branch.
    """
    results = []
    for step in dispatches:
        result = ctx.dispatch_tool(step["tool"], step["arguments"])
        results.append(result)
        if isinstance(result, dict) and result.get("error"):
            if on_error is not None:
                try:
                    ctx.dispatch_tool(on_error["tool"], on_error["arguments"])
                except Exception:  # noqa: BLE001 — cleanup must not mask the real failure
                    pass
            raise GitFlowError(f"dispatch {step['tool']} failed: {result['error']}")
    return results


def execute_locally(dispatches: list[dict]) -> list[dict]:
    """Test-only executor: real git for command steps, simulated PR tool."""
    results: list[dict] = []
    for step in dispatches:
        args = step["arguments"]
        command = args.get("command")
        if command:
            proc = subprocess.run(
                shlex.split(command), capture_output=True, text=True, timeout=60
            )
            if proc.returncode != 0:
                raise GitFlowError(f"local git step failed: {command}\n{proc.stderr}")
            results.append({"exit_code": 0, "stdout": proc.stdout})
        else:
            results.append(
                {
                    "simulated": True,
                    "url": f"local://pr/{args.get('head', 'unknown')}",
                    "head": args.get("head"),
                    "base": args.get("base"),
                }
            )
    return results
