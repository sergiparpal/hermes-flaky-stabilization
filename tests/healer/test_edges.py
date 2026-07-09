"""Phase 6 hardening: edge cases the happy paths never touch."""

import io
import json
import os
import stat
import zipfile

import handlers
from conftest import FIXTURES, FakeSandbox, FakeTransport, make_run_report
from flaky_healer.sandbox.subproc import SubprocessSandbox

REPO = "acme/webshop"
RUN_ID = "9876543210"
BASE = "https://api.github.com"


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _gh_transport(logs_body: bytes) -> FakeTransport:
    t = FakeTransport()
    t.add(
        "GET",
        f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}",
        json.loads((FIXTURES / "gh" / "run.json").read_text()),
    )
    t.add(
        "GET",
        f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}/jobs?per_page=100&page=1",
        json.loads((FIXTURES / "gh" / "jobs.json").read_text()),
    )
    t.add("GET", f"{BASE}/repos/{REPO}/actions/runs/{RUN_ID}/logs", logs_body)
    return t


class TestLogsZipEdges:
    def test_empty_logs_zip_returns_tail_note(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        transport = _gh_transport(
            _zip_bytes({"e2e-tests/1_Set up job.txt": "all quiet\nnothing failed here\n"})
        )
        data = json.loads(
            handlers.fetch_ci_logs({"build_id": RUN_ID, "repo": REPO}, transport=transport)
        )
        assert data["anchor_count"] == 0
        assert "no failure anchors found" in data["note"]

    def test_zip_without_txt_entries(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        transport = _gh_transport(_zip_bytes({"weird.bin": "\x00\x01"}))
        data = json.loads(
            handlers.fetch_ci_logs({"build_id": RUN_ID, "repo": REPO}, transport=transport)
        )
        assert "error" not in data
        assert data["anchor_count"] == 0

    def test_corrupt_logs_zip_is_json_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        transport = _gh_transport(b"definitely not a zip")
        data = json.loads(
            handlers.fetch_ci_logs({"build_id": RUN_ID, "repo": REPO}, transport=transport)
        )
        assert "not a valid zip" in data["error"]


class TestSandboxTimeout:
    def test_subprocess_timeout_kills_session_and_flags_run(self, tmp_path, monkeypatch):
        # a fake `npx` that ignores its args and sleeps forever
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_npx = bin_dir / "npx"
        fake_npx.write_text("#!/bin/sh\nsleep 300\n")
        fake_npx.chmod(fake_npx.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

        project = tmp_path / "project"
        project.mkdir()
        (project / "marker.txt").write_text("x")

        report = SubprocessSandbox().run_test(project, "tests/x.spec.ts", repeats=1, timeout_s=1)
        assert report.failed == 1
        assert report.to_dict()["timed_out_runs"] == 1
        assert report.results[0].exit_code == -1
        assert report.results[0].timed_out is True


class TestHealEdges:
    def test_heal_with_corrupt_trace_is_json_error(self, tmp_path):
        bad = tmp_path / "bad.trace.zip"
        bad.write_bytes(b"junk")
        data = json.loads(
            handlers.heal_flaky_test(
                {"repo_dir": str(tmp_path), "test_id": "x.spec.ts", "trace_path": str(bad)}
            )
        )
        assert "trace.zip" in data["error"]

    def test_heal_surfaces_sandbox_timeouts_as_unstable(self):
        from conftest import TOY_APP
        from flaky_healer.sandbox.base import RunResult, aggregate

        timed_out_burnin = aggregate(
            [RunResult(exit_code=-1, duration_s=180.0, timed_out=True) for _ in range(2)],
            "fake",
        )
        data = json.loads(
            handlers.heal_flaky_test(
                {
                    "repo_dir": str(TOY_APP),
                    "test_id": "tests/flaky-selector.spec.ts",
                    "trace_path": str(FIXTURES / "traces" / "selector.trace.zip"),
                },
                sandbox=FakeSandbox(
                    scripted=[make_run_report(5, failures=2), timed_out_burnin]
                ),
            )
        )
        report = data["report"]
        assert report["stable"] is False
        assert report["burnin"]["timed_out_runs"] == 2


class TestHealCommand:
    def test_command_registered_guarded(self, fake_ctx, plugin_module):
        plugin_module.register(fake_ctx)
        assert "heal" in fake_ctx.commands
        assert "/heal" in fake_ctx.commands["heal"]["description"]

    def test_command_happy_path_summary(self, fake_ctx):
        from conftest import TOY_APP

        trace = FIXTURES / "traces" / "selector.trace.zip"
        out = handlers.heal_command(
            f"{TOY_APP} tests/flaky-selector.spec.ts suggest trace_path={trace}",
            ctx=fake_ctx,
            sandbox=FakeSandbox(
                scripted=[make_run_report(5, failures=2), make_run_report(10, failures=0)]
            ),
        )
        assert "strategy: testid_selector" in out
        assert "burn-in: 10/10 green" in out
        assert "stable: True" in out
        assert ".getByTestId('submit')" in out

    def test_command_usage_message(self):
        assert handlers.heal_command("just-one-arg").startswith("usage:")
