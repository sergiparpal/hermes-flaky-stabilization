"""Git/PR write path (plan §4.7).

Every write step (branch, apply, commit, push, PR) is expressed as a
``ctx.dispatch_tool`` invocation so the host's approval/redaction/budget
pipeline governs it — this module NEVER pushes via raw subprocess on the
plugin's behalf. ``execute_locally`` is the pure-local path used by tests
(and only by tests): it executes the same dispatch list against a local
clone + ``file://`` bare remote and simulates the PR-creation tool.

Flow contract:

* ``run_preflight`` (read-only, also via dispatch) refuses to start on a
  dirty tree and records the user's original ref plus the validated HEAD.
* The fix branch is cut from the CURRENT HEAD — the commit burn-in actually
  validated — never from ``base``, which stays only the PR target.
* On any failure the cleanup dispatches restore the original ref and delete
  the half-built fix branch, so a retry never trips over stale state.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

PROTECTED_BRANCHES = {"main", "master", "develop", "trunk", "release"}
_BRANCH_PREFIX = "fix/flaky-"

_SHA_RE = re.compile(r"[0-9a-f]{7,64}")
# Refs we are willing to restore in cleanup: conservative git-ref charset.
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


class GitFlowError(RuntimeError):
    pass


@dataclass
class Preflight:
    """Read-only facts gathered before any write dispatch is emitted."""

    original_ref: str  # branch to restore on failure (commit sha when detached)
    head_sha: str  # the validated commit the fix branch is cut from


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


def _git_dispatch(repo: str, cmd: str, git_tool: str) -> dict:
    return {"tool": git_tool, "arguments": {"command": f"git -C {shlex.quote(repo)} {cmd}"}}


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
        return _git_dispatch(repo, cmd, git_tool)

    return [
        # Cut the branch from the CURRENT HEAD: burn-in validated the tree as it
        # stands, so cutting from `base` could publish a never-validated
        # combination (or fail the apply outright). `base` stays only the PR
        # target. -B (not -b) makes retries idempotent when a failed earlier
        # attempt left the deterministic branch name behind.
        git(f"checkout -B {q(branch)}"),
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


def cleanup_dispatches(
    repo_dir: str | Path,
    original_ref: str,
    branch: str | None = None,
    git_tool: str | None = None,
) -> list[dict]:
    """Best-effort failure cleanup: put the user back where they started.

    The PR flow only starts on a clean tree (``run_preflight``), so any local
    changes present mid-sequence came from our own ``git apply --index`` —
    ``checkout -f`` discards exactly those and never user work. The fix
    branch is then deleted so the next attempt starts from a blank slate.
    """
    git_tool = git_tool or config.git_tool()
    repo = str(Path(repo_dir).resolve())
    steps = [_git_dispatch(repo, f"checkout -f {shlex.quote(original_ref)}", git_tool)]
    if branch:
        steps.append(_git_dispatch(repo, f"branch -D {shlex.quote(branch)}", git_tool))
    return steps


def _result_failure(result) -> str | None:
    """``None`` when *result* positively signals success, else a failure reason.

    Hosts return heterogeneous shapes from ``dispatch_tool``; a step counts as
    successful ONLY when success is positively recognizable — otherwise a host
    returning ``{"exit_code": 128, "stderr": "fatal: ..."}`` (the very shape
    ``execute_locally`` produces) or a JSON string would read as success and
    the flow would commit on whatever branch the user has checked out.
    """
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return f"unrecognizable dispatch result: {result[:200]!r}"
        if not isinstance(parsed, dict):
            return f"unrecognizable dispatch result: {result[:200]!r}"
        result = parsed
    if not isinstance(result, dict):
        return f"unrecognizable dispatch result of type {type(result).__name__}"
    if result.get("error"):
        return str(result["error"])
    for key in ("exit_code", "returncode", "exitCode", "returnCode"):
        if key in result and result[key] is not None:
            try:
                code = int(result[key])
            except (TypeError, ValueError):
                return f"unrecognizable {key} in dispatch result: {result[key]!r}"
            if code != 0:
                detail = str(result.get("stderr") or result.get("stdout") or "")[:500]
                return f"{key}={code}" + (f": {detail}" if detail else "")
    return None


def _result_text(result) -> str | None:
    """Best-effort textual output of a read-only git dispatch, or ``None``."""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return result  # raw stdout from a plain-text host
        if not isinstance(parsed, dict):
            return result
        result = parsed
    if isinstance(result, dict):
        for key in ("stdout", "output", "text", "content"):
            value = result.get(key)
            if isinstance(value, str):
                return value
    return None


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def run_preflight(ctx, repo_dir: str | Path, git_tool: str | None = None) -> Preflight:
    """Read-only preflight gate for the PR flow, all via ``dispatch_tool``.

    1. REFUSE to start the git write sequence on a dirty tree: a dirty tree is
       exactly what makes ``git apply --index`` fail, and the old forced
       cleanup checkout would have destroyed the user's uncommitted changes.
    2. Record the original ref so failure cleanup restores where the user
       started (never a blind checkout of ``base``).
    3. Record the validated HEAD sha (branch cut point, shown in the PR body).

    Fail-closed: unreadable/unrecognizable output refuses the flow.
    """
    git_tool = git_tool or config.git_tool()
    repo = str(Path(repo_dir).resolve())

    def read(cmd: str, what: str) -> str:
        step = _git_dispatch(repo, cmd, git_tool)
        try:
            result = ctx.dispatch_tool(step["tool"], step["arguments"])
        except Exception as exc:
            raise GitFlowError(
                f"preflight `git {cmd}` raised {type(exc).__name__}: {exc}"
            ) from exc
        failure = _result_failure(result)
        if failure is not None:
            raise GitFlowError(f"preflight `git {cmd}` failed: {failure}")
        text = _result_text(result)
        if text is None:
            raise GitFlowError(
                f"preflight `git {cmd}` returned no readable output; refusing to start "
                f"the PR git sequence without {what}"
            )
        return text

    status = read("status --porcelain", "a clean-tree check")
    if status.strip():
        raise GitFlowError(
            f"refusing to start the PR git flow: the working tree at {repo} has "
            f"uncommitted changes:\n{status.strip()[:1000]}\n"
            "Commit or stash them and retry (mode='suggest' works on a dirty tree)."
        )

    head_sha = _first_line(read("rev-parse HEAD", "the validated HEAD commit")).lower()
    if not _SHA_RE.fullmatch(head_sha):
        raise GitFlowError(f"preflight could not resolve HEAD to a commit: {head_sha[:100]!r}")

    original_ref = _first_line(read("rev-parse --abbrev-ref HEAD", "the current branch"))
    if not original_ref or original_ref == "HEAD":
        original_ref = head_sha  # detached HEAD: restore by commit sha
    elif not _REF_RE.fullmatch(original_ref):
        raise GitFlowError(
            f"preflight returned an implausible branch name: {original_ref[:100]!r}"
        )
    return Preflight(original_ref=original_ref, head_sha=head_sha)


def execute_via_ctx(ctx, dispatches: list[dict], *, on_error=None) -> list:
    """Run the sequence through the host approval pipeline; stop on failure.

    A step is failed unless its result positively signals success (see
    ``_result_failure``) — including when ``dispatch_tool`` itself RAISES
    (approval denied by exception, transport failure). In every failure case
    the ``on_error`` cleanup dispatch(es) (a single dispatch dict or a list)
    are best-effort executed first, so a half-applied sequence doesn't strand
    the user mid-flow; then a GitFlowError is raised.
    """
    if isinstance(on_error, dict):
        cleanup_steps = [on_error]
    else:
        cleanup_steps = list(on_error or [])

    def _cleanup() -> None:
        for step in cleanup_steps:
            try:
                ctx.dispatch_tool(step["tool"], step["arguments"])
            except Exception:  # noqa: BLE001 — cleanup must not mask the real failure
                pass

    results = []
    for step in dispatches:
        try:
            result = ctx.dispatch_tool(step["tool"], step["arguments"])
        except Exception as exc:
            _cleanup()
            raise GitFlowError(
                f"dispatch {step['tool']} raised {type(exc).__name__}: {exc}"
            ) from exc
        results.append(result)
        failure = _result_failure(result)
        if failure is not None:
            _cleanup()
            raise GitFlowError(f"dispatch {step['tool']} failed: {failure}")
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
