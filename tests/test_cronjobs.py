"""The shared no-agent cron-job installer (``cronjobs``).

Both `install-cron` and `install-cron --with-jira-sync` route through this module.
It used to exist twice — factored into helpers in ``detective.cli`` and re-inlined
in ``cli._install_jira_sync_job`` — and the copies had drifted. These tests pin
the behaviour the re-inlined copy had lost.
"""

from __future__ import annotations

import os
import shlex
import stat

import pytest

from hermes_flaky_stabilization import cronjobs

SCAN_JOB = cronjobs.CronJob("flaky-stab-scan.sh", "flaky-stabilization", "cron job")
JIRA_JOB = cronjobs.CronJob(
    "flaky-stab-jira-sync.sh", "flaky-stabilization-jira-sync", "jira-sync job"
)


# ---------------------------------------------------------------------------
# shim source resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job", [SCAN_JOB, JIRA_JOB])
def test_shim_source_resolves_to_the_shipped_script(job):
    """One derivation of `<checkout>/scripts/`, not `parents[1]` in one caller and
    `parents[2]` in another (which agreed only because the files sat at different
    depths)."""
    src = cronjobs.shim_source(job.shim_name)
    assert src.is_file(), f"{job.shim_name} is not shipped at {src}"
    assert src.parent.name == "scripts"


def test_missing_shim_error_reports_and_creates_nothing(profile_env):
    problem = cronjobs.missing_shim_error("no-such-shim.sh")
    assert problem and "no-such-shim.sh" in problem
    assert not (profile_env / "scripts").exists(), "the check must not mkdir anything"


def test_missing_shim_error_is_none_when_present():
    assert cronjobs.missing_shim_error(SCAN_JOB.shim_name) is None


# ---------------------------------------------------------------------------
# install_shim: the 0700 dir + 0700 script the re-inlined copy had lost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job", [SCAN_JOB, JIRA_JOB])
def test_install_shim_locks_down_both_the_dir_and_the_script(profile_env, job):
    """The gateway executes what lands here on a schedule, so another local user
    must not be able to read — let alone replace — either.

    ``cli._install_jira_sync_job`` used to mkdir the directory without chmod'ing
    it; that was masked only because the detection job's installer always ran
    first. Called on its own, as here, the omission is visible.
    """
    assert not (profile_env / "scripts").exists()

    shim_dst = cronjobs.install_shim(job.shim_name)

    scripts_dir = profile_env / "scripts"
    assert stat.S_IMODE(os.stat(scripts_dir).st_mode) == 0o700, "scripts dir not 0700"
    assert stat.S_IMODE(os.stat(shim_dst).st_mode) == 0o700, "shim not 0700"
    assert shim_dst == scripts_dir / job.shim_name
    assert shim_dst.read_text(encoding="utf-8") == \
        cronjobs.shim_source(job.shim_name).read_text(encoding="utf-8")


def test_install_shim_tightens_a_preexisting_world_readable_dir(profile_env):
    """A scripts/ dir left at 0755 by an earlier install is repaired, not accepted."""
    scripts_dir = profile_env / "scripts"
    scripts_dir.mkdir(parents=True)
    os.chmod(scripts_dir, 0o755)

    cronjobs.install_shim(SCAN_JOB.shim_name)

    assert stat.S_IMODE(os.stat(scripts_dir).st_mode) == 0o700


def test_install_shim_raises_oserror_for_the_caller_to_shape(profile_env, monkeypatch):
    """Callers turn this into a one-line error + exit 1, never a traceback."""
    def boom(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr("shutil.copyfile", boom)
    with pytest.raises(OSError, match="denied"):
        cronjobs.install_shim(SCAN_JOB.shim_name)


# ---------------------------------------------------------------------------
# the printed copy-paste command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("job", [SCAN_JOB, JIRA_JOB])
def test_printable_command_shell_quotes_user_supplied_parts(job):
    printable = cronjobs.printable_command(job, "*/5 9 * * *", "my channel")
    tokens = shlex.split(printable)
    assert "*/5 9 * * *" in tokens        # the whole cron expr stays one argument
    assert "my channel" in tokens         # the delivery channel stays one argument
    assert tokens[:3] == ["hermes", "cron", "create"]
    assert tokens[-2:] == ["--name", job.job_name]
    assert "--script" in tokens and job.shim_name in tokens


# ---------------------------------------------------------------------------
# create_job: never an error, always a usable fallback
# ---------------------------------------------------------------------------


def _fake_completed(returncode, stdout="", stderr=""):
    from types import SimpleNamespace

    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_create_job_success(monkeypatch, capsys):
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: _fake_completed(0, stdout="ok"))
    assert cronjobs.create_job(JIRA_JOB, "0 9 * * *", "local") == 0
    out = capsys.readouterr().out
    assert f"created cron job '{JIRA_JOB.job_name}'" in out


def test_create_job_nonzero_falls_back_to_printing_with_the_job_description(
    monkeypatch, capsys
):
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: _fake_completed(1, stderr="gateway not configured"))
    assert cronjobs.create_job(JIRA_JOB, "0 9 * * *", "local") == 0
    out = capsys.readouterr().out
    assert "gateway not configured" in out
    assert "could not create the jira-sync job automatically" in out
    assert "hermes cron create" in out
    assert "gateway" in out.lower()      # the gateway note the inlined copy omitted


def test_create_job_missing_hermes_cli_falls_back(monkeypatch, capsys):
    def boom(*a, **k):
        raise FileNotFoundError("no hermes")

    monkeypatch.setattr("subprocess.run", boom)
    assert cronjobs.create_job(SCAN_JOB, "0 9 * * *", "local") == 0
    out = capsys.readouterr().out
    assert "could not run the hermes CLI" in out
    assert "hermes cron create" in out
