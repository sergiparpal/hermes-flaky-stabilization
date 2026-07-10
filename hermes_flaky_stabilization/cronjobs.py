"""Installer for the plugin's no-agent cron jobs (the detection scan, the Jira sync).

Both jobs follow the same recipe: resolve the shim shipped in the checkout's
``scripts/``, copy it into ``<hermes_home>/scripts/`` owner-only, then create the
job once via the single sanctioned ``hermes cron create`` subprocess — falling
back to *printing* the command when the hermes CLI is absent or the gateway is
not configured.

That recipe used to exist twice: ``detective.cli`` factored it into helpers, and
``cli._install_jira_sync_job`` re-inlined it. The copies had already drifted —
the re-inlined one never chmod'd ``<hermes_home>/scripts/`` to 0700, and it
derived the shim source with ``parents[1]`` where the original used
``parents[2]`` (both happened to land on the checkout root only because the two
files sit at different depths). It also copied the shim with no existence check
and no ``OSError`` guard, so a site-packages install without ``scripts/`` raised
an uncaught ``FileNotFoundError`` out of the CLI.

The shim source is derived from *this* module's location, once.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR_NAME = "scripts"


@dataclass(frozen=True)
class CronJob:
    """One installable no-agent job.

    Built at *call* time from the caller's module constants, never frozen at
    import: ``tests`` monkeypatch ``cli.SHIM_NAME`` to simulate a missing shim.
    """

    shim_name: str   # e.g. "flaky-stab-scan.sh"
    job_name: str    # e.g. "flaky-stabilization" — the `hermes cron` job id
    description: str  # e.g. "cron job" — used in the operator-facing messages


def shim_source(shim_name: str) -> Path:
    """``<plugin checkout>/scripts/<shim_name>``."""
    return Path(__file__).resolve().parents[1] / SCRIPTS_DIR_NAME / shim_name


def missing_shim_error(shim_name: str) -> str | None:
    """``None`` when the shim source exists, else the operator-facing message.

    Callers check this *before* persisting anything, so a broken install (e.g.
    site-packages without the checkout's ``scripts/``) fails cleanly rather than
    leaving a half-written config behind. Creates nothing.
    """
    src = shim_source(shim_name)
    if src.exists():
        return None
    return (f"cron shim source not found at {src}; reinstall the plugin from a "
            f"checkout that ships {SCRIPTS_DIR_NAME}/{shim_name}, or copy that "
            f"script into <hermes_home>/{SCRIPTS_DIR_NAME}/ yourself.")


def install_shim(shim_name: str) -> Path:
    """Copy the shim into ``<hermes_home>/scripts/``; return its path.

    Both the directory and the script are 0700: the gateway executes what lands
    here on a schedule, so another local user must not be able to read — let
    alone replace — either. Raises ``OSError`` on failure; callers turn that into
    a one-line error.
    """
    from . import paths

    scripts_dir = paths.get_hermes_home() / SCRIPTS_DIR_NAME  # must live here (§1.3)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(scripts_dir, 0o700)
    except OSError:
        pass  # best-effort: a filesystem without POSIX modes must not break install
    shim_dst = scripts_dir / shim_name
    shutil.copyfile(shim_source(shim_name), shim_dst)
    try:
        os.chmod(shim_dst, 0o700)
    except OSError:
        pass
    return shim_dst


def printable_command(job: CronJob, schedule: str, deliver: str) -> str:
    """The copy-paste ``hermes cron create`` command for *job*.

    ``shlex.quote`` the user-supplied parts so the printed command stays a single
    valid shell command even when a schedule or delivery channel contains spaces
    or shell metacharacters. (Real job creation uses argv, so it was never
    injectable; this only hardens the human-facing fallback string.)
    """
    return (f"hermes cron create {shlex.quote(schedule)} --no-agent "
            f"--script {job.shim_name} --deliver {shlex.quote(deliver)} "
            f"--name {job.job_name}")


def gateway_note() -> str:
    return ("Note: the gateway daemon must be running for the job to fire "
            "(`hermes gateway install`, then `hermes gateway`).")


def print_manual_command(printable: str) -> None:
    """Print the manual ``hermes cron create`` command plus the gateway note."""
    print(f"  {printable}")
    print(gateway_note())


def create_job(job: CronJob, schedule: str, deliver: str) -> int:
    """The one sanctioned subprocess: create *job* once. Always returns 0.

    A missing hermes CLI or an unconfigured gateway is not an error — it prints
    the exact command to run by hand. (A cron-run session cannot create cron
    jobs, so this is only ever reached from the standalone CLI.)
    """
    printable = printable_command(job, schedule, deliver)
    cron_cmd = ["hermes", "cron", "create", schedule, "--no-agent",
                "--script", job.shim_name, "--deliver", deliver,
                "--name", job.job_name]
    try:
        result = subprocess.run(cron_cmd, capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        print(f"could not run the hermes CLI ({exc}). To create the job, run:")
        print_manual_command(printable)
        return 0

    if result.returncode == 0:
        out = (result.stdout or "").strip()
        if out:
            print(out)
        print(f"created cron job '{job.job_name}' (verify with `hermes cron list`).")
        return 0

    # Non-zero (e.g. gateway not configured): fall back to printing the command.
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        print(detail)
    print(f"could not create the {job.description} automatically. To create it, run:")
    print_manual_command(printable)
    return 0
