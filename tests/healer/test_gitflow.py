"""Git/PR flow tests against a temp local clone + bare file:// remote.

Property under test (plan §7): write operations exist ONLY as dispatch_tool
payloads, the branch is always fix/flaky-<test>-<sig8>, and nothing ever
pushes a default branch.
"""

import subprocess

import pytest
from conftest import TOY_APP
from flaky_healer.gitflow import (
    GitFlowError,
    branch_name,
    build_dispatches,
    execute_locally,
    execute_via_ctx,
    slugify_test_id,
)

SIG = "deadbeefcafe0123456789aa"


def _git(repo, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def git_world(tmp_path):
    """A working clone (with the flaky spec committed) + a bare origin."""
    work = tmp_path / "checkout"
    (work / "tests").mkdir(parents=True)
    spec = TOY_APP / "tests" / "flaky-selector.spec.ts"
    (work / "tests" / "flaky-selector.spec.ts").write_text(spec.read_text())
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.email", "healer@test.local")
    _git(work, "config", "user.name", "Healer Test")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "baseline")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    _git(work, "remote", "add", "origin", f"file://{origin}")
    _git(work, "push", "-q", "-u", "origin", "main")
    return work, origin


def _patch_file(tmp_path, work):
    """A real unified diff for the spec, in git-apply format."""
    import difflib

    rel = "tests/flaky-selector.spec.ts"
    before = (work / rel).read_text()
    after = before.replace(".locator('#btn-1f9c')", ".getByTestId('submit')")
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )
    patch = tmp_path / "heal.patch"
    patch.write_text(diff)
    return patch


def test_slug_and_branch_name():
    assert slugify_test_id("tests/flaky-selector.spec.ts") == "flaky-selector"
    assert branch_name("tests/flaky-selector.spec.ts", SIG) == "fix/flaky-flaky-selector-deadbeef"


@pytest.mark.parametrize("bad_branch", ["main", "master", "fix/flaky-x", "feature/x"])
def test_branch_guards(tmp_path, bad_branch):
    kwargs = dict(
        repo_dir=tmp_path,
        patch_path=tmp_path / "p.patch",
        commit_message="m",
        pr_title="t",
        pr_body="b",
    )
    if bad_branch == "fix/flaky-x":  # valid prefix, equal to base -> still refused
        with pytest.raises(GitFlowError):
            build_dispatches(branch=bad_branch, base="fix/flaky-x", **kwargs)
    else:
        with pytest.raises(GitFlowError):
            build_dispatches(branch=bad_branch, base="main", **kwargs)


def test_dispatch_sequence_shape(tmp_path):
    branch = branch_name("tests/flaky-selector.spec.ts", SIG)
    dispatches = build_dispatches(
        repo_dir=tmp_path,
        branch=branch,
        base="main",
        patch_path=tmp_path / "p.patch",
        commit_message="fix(flaky): msg",
        pr_title="title",
        pr_body="body",
    )
    tools = [d["tool"] for d in dispatches]
    assert tools == ["terminal", "terminal", "terminal", "terminal", "create_pull_request"]
    cmds = [d["arguments"].get("command", "") for d in dispatches[:4]]
    assert "checkout -b" in cmds[0] and branch in cmds[0]
    assert "apply --index" in cmds[1]
    assert "commit -m" in cmds[2]
    assert f"push -u origin {branch}" in cmds[3]
    assert all("push -u origin main" not in c for c in cmds)
    pr = dispatches[4]["arguments"]
    assert pr["head"] == branch and pr["base"] == "main"


def test_tool_names_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("FLAKY_HEALER_GIT_TOOL", "run_shell")
    monkeypatch.setenv("FLAKY_HEALER_PR_TOOL", "github_open_pr")
    dispatches = build_dispatches(
        repo_dir=tmp_path,
        branch="fix/flaky-x-aaaaaaaa",
        base="main",
        patch_path=tmp_path / "p.patch",
        commit_message="m",
        pr_title="t",
        pr_body="b",
    )
    assert {d["tool"] for d in dispatches} == {"run_shell", "github_open_pr"}


def test_local_execution_against_bare_remote(tmp_path, git_world):
    work, origin = git_world
    patch = _patch_file(tmp_path, work)
    branch = branch_name("tests/flaky-selector.spec.ts", SIG)
    main_sha_before = _git(origin, "rev-parse", "main")

    dispatches = build_dispatches(
        repo_dir=work,
        branch=branch,
        base="main",
        patch_path=patch,
        commit_message="fix(flaky): heal tests/flaky-selector.spec.ts via testid_selector",
        pr_title="fix(flaky): tests/flaky-selector.spec.ts",
        pr_body="automated heal",
    )
    results = execute_locally(dispatches)

    # branch exists on the remote, content is the patched spec
    assert branch in _git(origin, "branch", "--list", branch)
    patched = _git(origin, "show", f"{branch}:tests/flaky-selector.spec.ts")
    assert ".getByTestId('submit')" in patched
    assert ".locator('#btn-1f9c')" not in patched
    # commit content equals the patch ops, message matches
    assert "fix(flaky): heal" in _git(origin, "log", "-1", "--format=%s", branch)
    # main untouched on the remote
    assert _git(origin, "rev-parse", "main") == main_sha_before
    # PR step simulated, never a real network call
    assert results[-1]["simulated"] is True
    assert results[-1]["head"] == branch


def test_local_execution_failure_raises(tmp_path, git_world):
    work, _origin = git_world
    bogus_patch = tmp_path / "bogus.patch"
    bogus_patch.write_text("not a valid diff\n")
    dispatches = build_dispatches(
        repo_dir=work,
        branch="fix/flaky-broken-00000000",
        base="main",
        patch_path=bogus_patch,
        commit_message="m",
        pr_title="t",
        pr_body="b",
    )
    with pytest.raises(GitFlowError, match="local git step failed"):
        execute_locally(dispatches)


def test_execute_via_ctx_stops_on_error(fake_ctx):
    fake_ctx.dispatch_results["terminal"] = {"error": "denied by approval policy"}
    dispatches = [
        {"tool": "terminal", "arguments": {"command": "git checkout -b x"}},
        {"tool": "terminal", "arguments": {"command": "git push"}},
    ]
    with pytest.raises(GitFlowError, match="denied by approval policy"):
        execute_via_ctx(fake_ctx, dispatches)
    assert len(fake_ctx.dispatched) == 1, "must stop at the first failed dispatch"
