"""Sandbox tests: factory selection, isolation flags, env scrubbing.

Real container runs carry @pytest.mark.docker (auto-skipped without a
reachable daemon); one real subprocess run exercises the fallback backend and
skips itself when node/browsers are absent.
"""

import os

import pytest
from conftest import TOY_APP, local_browsers_available
from flaky_healer import config
from flaky_healer.sandbox.base import (
    RunResult,
    SandboxError,
    copy_project,
    create_sandbox,
    docker_available,
    execute_phase,
    map_runs,
)
from flaky_healer.sandbox.docker import DockerSandbox
from flaky_healer.sandbox.subproc import SubprocessSandbox

DOCKER_OK = docker_available()

needs_docker = pytest.mark.docker
needs_local_browsers = pytest.mark.skipif(
    not local_browsers_available(),
    reason="node + toy-app node_modules + chromium required for subprocess sandbox runs",
)


class TestFactory:
    def test_env_forces_subprocess(self, monkeypatch):
        monkeypatch.setenv("FLAKY_HEALER_SANDBOX", "subprocess")
        assert isinstance(create_sandbox(), SubprocessSandbox)

    def test_explicit_prefer_argument_wins(self):
        assert isinstance(create_sandbox("subprocess"), SubprocessSandbox)

    def test_unknown_backend_rejected(self):
        with pytest.raises(SandboxError, match="unknown sandbox backend"):
            create_sandbox("firecracker")

    def test_docker_request_without_daemon_raises(self, monkeypatch):
        monkeypatch.setattr(
            "flaky_healer.sandbox.base.docker_available", lambda *a, **k: False
        )
        with pytest.raises(SandboxError, match="no reachable Docker daemon"):
            create_sandbox("docker")

    def test_auto_resolves_to_an_available_backend(self):
        sandbox = create_sandbox("auto")
        expected = DockerSandbox if DOCKER_OK else SubprocessSandbox
        assert isinstance(sandbox, expected)


class TestDockerCommand:
    def test_isolation_flags_present(self, tmp_path):
        sandbox = DockerSandbox(image="mcr.microsoft.com/playwright:v1.60.0-noble")
        cmd = sandbox.build_command(tmp_path, "tests/stable.spec.ts", "cname")
        joined = " ".join(cmd)
        assert "--network=none" in cmd
        assert "--memory=2g" in cmd
        assert "--cpus=2" in cmd
        assert "--pids-limit=256" in cmd
        assert f"--user={os.getuid()}:{os.getgid()}" in cmd
        assert "-e CI=1" in joined and "-e HOME=/tmp" in joined
        assert f"{tmp_path.resolve()}:/work" in joined
        assert "npx" in cmd and "playwright" in cmd and "test" in cmd
        assert "--trace=off" in cmd and "--retries=0" in cmd and "--workers=1" in cmd
        assert cmd.index("mcr.microsoft.com/playwright:v1.60.0-noble") < cmd.index("npx")

    def test_no_host_env_forwarding(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUPER_SECRET", "leak-me")
        sandbox = DockerSandbox()
        cmd = sandbox.build_command(tmp_path, "t.spec.ts", "cname")
        assert "SUPER_SECRET" not in " ".join(cmd)
        env_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
        assert env_args == ["CI=1", "HOME=/tmp"]


class TestSubprocessEnvScrub:
    def test_env_built_from_scratch(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
        monkeypatch.setenv("GITHUB_TOKEN", "leak-me-too")
        env = SubprocessSandbox().build_env(tmp_path)
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert env["CI"] == "1"
        assert env["HOME"] == str(tmp_path)
        assert "PATH" in env
        allowed = {"PATH", "CI", "HOME", "PLAYWRIGHT_BROWSERS_PATH"}
        assert set(env) <= allowed

    def test_explicit_allowlist_passes_named_vars(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LD_LIBRARY_PATH", "/custom/libs")
        monkeypatch.setenv("NOT_LISTED", "nope")
        monkeypatch.setenv("FLAKY_HEALER_SUBPROC_PASS_ENV", "LD_LIBRARY_PATH")
        env = SubprocessSandbox().build_env(tmp_path)
        assert env["LD_LIBRARY_PATH"] == "/custom/libs"
        assert "NOT_LISTED" not in env


class TestCopyProject:
    def test_excludes_vcs_and_artifacts_keeps_node_modules(self, tmp_path):
        src = tmp_path / "src"
        (src / ".git").mkdir(parents=True)
        (src / ".git" / "HEAD").write_text("ref: x")
        (src / "test-results").mkdir()
        (src / "test-results" / "old.zip").write_text("stale")
        (src / "node_modules" / "dep").mkdir(parents=True)
        (src / "node_modules" / "dep" / "index.js").write_text("ok")
        (src / "server.js").write_text("ok")
        dst = copy_project(src, tmp_path / "dst")
        assert (dst / "server.js").is_file()
        assert (dst / "node_modules" / "dep" / "index.js").is_file()
        assert not (dst / ".git").exists()
        assert not (dst / "test-results").exists()

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(SandboxError, match="does not exist"):
            copy_project(tmp_path / "ghost", tmp_path / "dst")

    def test_default_byte_copies_distinct_inodes(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "a.txt"
        f.write_text("payload")
        dst = copy_project(src, tmp_path / "dst")
        assert (dst / "a.txt").read_text() == "payload"
        assert (dst / "a.txt").stat().st_ino != f.stat().st_ino

    def test_hardlinks_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLAKY_HEALER_HARDLINK_COPY", "1")
        src = tmp_path / "src"
        (src / "node_modules" / "dep").mkdir(parents=True)
        f = src / "node_modules" / "dep" / "index.js"
        f.write_text("payload")
        dst = copy_project(src, tmp_path / "dst")
        linked = dst / "node_modules" / "dep" / "index.js"
        assert linked.read_text() == "payload"
        # same filesystem (tmp) → hardlinked to the source inode, not byte-copied
        assert linked.stat().st_ino == f.stat().st_ino


class TestRunConcurrencyConfig:
    def test_default_is_one(self):
        assert config.run_concurrency() == 1

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("FLAKY_HEALER_RUN_CONCURRENCY", "4")
        assert config.run_concurrency() == 4

    @pytest.mark.parametrize("bad", ["lots", "", "0", "-3"])
    def test_invalid_or_sub_one_falls_back_to_one(self, monkeypatch, bad):
        monkeypatch.setenv("FLAKY_HEALER_RUN_CONCURRENCY", bad)
        assert config.run_concurrency() == 1


class TestMapRuns:
    def test_serial_when_concurrency_one(self):
        assert map_runs([lambda: 1, lambda: 2, lambda: 3], 1) == [1, 2, 3]

    def test_parallel_preserves_submission_order(self):
        thunks = [(lambda i=i: i * i) for i in range(6)]
        assert map_runs(thunks, 4) == [0, 1, 4, 9, 16, 25]

    def test_thunk_exception_propagates(self):
        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            map_runs([lambda: 1, boom, lambda: 3], 3)


class TestExecutePhase:
    """The serial/parallel orchestrator shared by both real backends, exercised
    here with a fake run_one so it needs neither Docker nor browsers."""

    @staticmethod
    def _project(tmp_path):
        src = tmp_path / "proj"
        (src / "tests").mkdir(parents=True)
        (src / "tests" / "t.spec.ts").write_text("// spec\n")
        return src

    def test_serial_makes_one_shared_copy(self, tmp_path):
        works = []

        def run_one(work, home):
            works.append(str(work))
            assert home is None
            return RunResult(exit_code=0, duration_s=0.0)

        report = execute_phase(self._project(tmp_path), 3, 1, "fake", None, run_one)
        assert report.runs == 3 and report.all_green
        assert len(set(works)) == 1, "serial phase reuses a single copy across repeats"

    def test_parallel_isolates_copies_and_applies_prepare_first(self, tmp_path):
        prepared, works = [], []

        def prepare(work):
            prepared.append(str(work))
            (work / "patched.marker").write_text("x")

        def run_one(work, home):
            works.append(str(work))
            assert (work / "patched.marker").is_file(), "prepare runs before the run"
            return RunResult(exit_code=0, duration_s=0.0)

        report = execute_phase(self._project(tmp_path), 4, 4, "fake", prepare, run_one)
        assert report.runs == 4 and report.all_green
        assert len(set(works)) == 4, "each parallel run gets its own isolated copy"
        assert len(set(prepared)) == 4, "prepare patches every isolated copy"

    def test_with_home_provisions_a_home_dir(self, tmp_path):
        def run_one(work, home):
            assert home is not None and home.is_dir()
            return RunResult(exit_code=0, duration_s=0.0)

        report = execute_phase(self._project(tmp_path), 2, 1, "fake", None, run_one, with_home=True)
        assert report.runs == 2


@needs_local_browsers
class TestSubprocessReal:
    def test_stable_spec_green_via_subprocess(self, browser_env):
        report = SubprocessSandbox().run_test(TOY_APP, "tests/stable.spec.ts", repeats=1)
        assert report.isolation == "subprocess"
        assert report.all_green, report.to_dict()


@needs_docker
class TestDockerReal:
    def test_stable_spec_green_twice_in_docker(self):
        if not DOCKER_OK:
            pytest.skip("no reachable Docker daemon")
        report = DockerSandbox().run_test(TOY_APP, "tests/stable.spec.ts", repeats=2)
        assert report.isolation == "docker"
        assert report.runs == 2
        assert report.all_green, report.to_dict()
