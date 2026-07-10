"""Regression tests for the 2026-07 security audit fixes.

Each test pins a *specific, verified* bypass/weakness the audit found, using the
exact input strings that leaked before the fix, so a regression re-opens the
finding loudly. Grouped by the audit's priority tiers.
"""

from __future__ import annotations

import os
import pathlib
import stat

import pytest

# ---------------------------------------------------------------------------
# P1 — triage/redact.py secret-redaction bypasses (CI-log text → LLM)
# ---------------------------------------------------------------------------

class TestTriageRedactBypasses:
    def _redact(self):
        from hermes_flaky_stabilization.triage import redact
        return redact

    @pytest.mark.parametrize("line,secret", [
        ('{"password": "s3cr3tvalue"}', "s3cr3tvalue"),          # JSON-quoted
        ("{'api_key': 's3cr3tvalue'}", "s3cr3tvalue"),           # dict-quoted
        ('password = "correct horse battery"', "correct horse battery"),  # quoted multi-word
    ])
    def test_quoted_and_json_secrets_redacted(self, line, secret):
        out = self._redact().redact(line)
        assert secret not in out
        assert "<REDACTED>" in out

    @pytest.mark.parametrize("line,pw", [
        ("postgres://user:p4ssw0rd@db.internal:5432/app", "p4ssw0rd"),
        ("redis://:onlypass@cache:6379", "onlypass"),
        ("amqp://svc:tok3n@mq/vhost", "tok3n"),
    ])
    def test_url_embedded_credentials_redacted(self, line, pw):
        out = self._redact().redact(line)
        assert pw not in out
        assert "<REDACTED>" in out

    @pytest.mark.parametrize("safe", [
        "http://host:8080/path",              # port, no credentials
        "https://github.com/org/repo.git",    # plain URL
        "AssertionError: expected 5 but got 4",
        "process exited with code 137",
    ])
    def test_non_secrets_untouched(self, safe):
        assert self._redact().redact(safe) == safe


# ---------------------------------------------------------------------------
# P1 — healer CI logs are scrubbed before reaching the model
# ---------------------------------------------------------------------------

class TestHealerLogScrub:
    def test_ci_log_secrets_scrubbed_signal_kept(self):
        from hermes_flaky_stabilization.healer.flaky_healer import ci_logs
        log = (
            "AssertionError: expected 200 got 500\n"
            '+ curl -H "Authorization: Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n'
            "DATABASE_URL=postgres://svc:sup3rs3cret@db.internal:5432/app\n"
            'env {"api_key": "AKIAIOSFODNN7EXAMPLE"}\n'
        )
        out = ci_logs.scrub_ci_secrets(log)
        for secret in ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                       "sup3rs3cret", "AKIAIOSFODNN7EXAMPLE"):
            assert secret not in out
        assert "AssertionError" in out  # the failure signal survives


# ---------------------------------------------------------------------------
# P1 — PII detectors (gate) and redactor (outbound) recall gaps
# ---------------------------------------------------------------------------

class TestPiiDetectorRecall:
    @pytest.mark.parametrize("email", ["josé@example.com", "иван@example.com",
                                       "bob@example.com"])
    def test_gate_detects_internationalized_emails(self, email):
        from hermes_flaky_stabilization.pii import detectors
        assert [f.type for f in detectors.run_detectors(email)] == ["email"]

    @pytest.mark.parametrize("text,label", [
        ("email josé@example.com", "[redacted-email]"),
        ("SSN 123 45 6789", "[redacted-ssn]"),         # space-separated
        ("SSN 123-45-6789", "[redacted-ssn]"),         # control (dash)
        ("IPv6 2001:0db8:85a3:0000:0000:8a2e:0370:7334", "[redacted-ip]"),
        ("IPv6 short ::1", "[redacted-ip]"),
        ("IPv4 192.168.1.100", "[redacted-ip]"),       # control
    ])
    def test_outbound_redactor_covers_gaps(self, text, label):
        from hermes_flaky_stabilization.pii import redaction
        assert label in redaction.redact_text(text)

    @pytest.mark.parametrize("safe", ["build at 12:34:56 done", "mac 00:1a:2b:3c:4d:5e",
                                      "version 1.2.3", "date 2024-01-15 10:00"])
    def test_outbound_redactor_no_over_redaction(self, safe):
        from hermes_flaky_stabilization.pii import redaction
        assert redaction.redact_text(safe) == safe


class TestOutboundTicketEgress:
    def test_build_ticket_masks_scanner_only_pii(self):
        """The direct jira_create_incident path now runs the mask_pii egress
        canary (not just redact_text), so PII classes only the scanner catches
        are masked before the ticket leaves the machine."""
        from hermes_flaky_stabilization.incidents import write
        ticket = write.build_ticket(
            {"title": "leak josé@example.com",
             "body": "SSN 123 45 6789 host 2001:db8::1"},
            {"project_key": "INC", "issue_type": "Bug"},
        )
        blob = f"{ticket['title']} {ticket['body']}"
        assert "josé@example.com" not in blob
        assert "123 45 6789" not in blob
        assert "2001:db8::1" not in blob


# ---------------------------------------------------------------------------
# P2 — SQLite DB + WAL sidecar permission hardening
# ---------------------------------------------------------------------------

class TestPermissionHardening:
    def _mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_harden_db_files_covers_sidecars(self, tmp_path):
        from hermes_flaky_stabilization import paths
        base = tmp_path / "state.db"
        for suffix in ("", "-wal", "-shm", "-journal"):
            fd = os.open(str(base) + suffix, os.O_CREAT | os.O_WRONLY, 0o644)
            os.close(fd)
        paths.harden_db_files(base)
        for suffix in ("", "-wal", "-shm", "-journal"):
            assert self._mode(str(base) + suffix) == 0o600

    def test_precreate_private_owns_file_before_connect(self, tmp_path):
        from hermes_flaky_stabilization import paths
        p = tmp_path / "new.db"
        paths.precreate_private(p)
        assert p.exists() and self._mode(p) == 0o600

    def test_special_paths_are_noops(self):
        from hermes_flaky_stabilization import paths
        # Must not raise or try to touch a real file.
        paths.precreate_private(":memory:")
        paths.harden_db_files(":memory:")
        paths.harden_db_files("file:cache?mode=memory")

    def test_state_db_and_dir_are_owner_only(self, _isolated_env):
        from contextlib import closing

        from hermes_flaky_stabilization.storage import state
        with closing(state.connect()) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _t(x)")
            conn.commit()
        db = state.state_db_path()
        assert self._mode(db) == 0o600
        assert self._mode(db.parent) == 0o700


# ---------------------------------------------------------------------------
# P3 — defense-in-depth
# ---------------------------------------------------------------------------

class TestCronScheduleValidation:
    @pytest.mark.parametrize("good", ["0 9 * * *", "*/5 * * * *", "@daily",
                                      "0 0 1 JAN *", "0 9 * * MON"])
    def test_valid_schedules_accepted(self, good):
        from hermes_flaky_stabilization.detective import domain
        domain.validate_cron_schedule(good)  # no raise

    @pytest.mark.parametrize("bad", ["--name=evil", "-9 * * * *",
                                     "0 9 * * * ; rm -rf /", "$(whoami) * * * *",
                                     "0 9 * *", ""])
    def test_injection_and_malformed_rejected(self, bad):
        from hermes_flaky_stabilization.detective import domain
        with pytest.raises(ValueError):
            domain.validate_cron_schedule(bad)


class TestGitHubBaseHttpsOnly:
    def test_http_base_rejected(self):
        from hermes_flaky_stabilization.healer.flaky_healer.ci import base as ci_base
        from hermes_flaky_stabilization.healer.flaky_healer.ci.github_actions import (
            GitHubActionsCI,
        )
        with pytest.raises(ci_base.CIError, match="https"):
            GitHubActionsCI(token="t", api_base="http://internal.corp/api/v3")

    def test_https_base_accepted(self):
        from hermes_flaky_stabilization.healer.flaky_healer.ci.github_actions import (
            GitHubActionsCI,
        )
        # Construction must not raise for an https GitHub Enterprise base.
        GitHubActionsCI(token="t", api_base="https://ghe.corp.local/api/v3")


class TestLogfetchUrlLeak:
    def test_non_https_refusal_hides_query(self):
        from hermes_flaky_stabilization.triage import logfetch
        url = "http://ci.example.com/logs?token=SECRETVALUE123"
        with pytest.raises(logfetch.LogFetchError) as exc:
            logfetch.fetch_remote(url)
        assert "SECRETVALUE123" not in str(exc.value)


class TestSandboxArgTermination:
    def test_docker_command_terminates_options_before_test_id(self):
        pytest.importorskip("hermes_flaky_stabilization.healer.flaky_healer.sandbox.docker")
        from hermes_flaky_stabilization.healer.flaky_healer.sandbox.docker import (
            DockerSandbox,
        )
        cmd = DockerSandbox(image="img").build_command(pathlib.Path("."),
                                                       "--reporter=evil", "c")
        # `--` must directly precede the (flag-shaped) test_id, and every real
        # option must come before `--`.
        assert "--" in cmd
        dashdash = cmd.index("--")
        assert cmd[dashdash + 1] == "--reporter=evil"
        assert cmd.index("--reporter=line") < dashdash  # our own flags precede it
