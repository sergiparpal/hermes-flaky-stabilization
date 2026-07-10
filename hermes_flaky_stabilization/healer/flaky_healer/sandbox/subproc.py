"""Subprocess sandbox — the documented WEAKER fallback (plan §4.5).

Isolation here is environment scrubbing + per-run timeout + basic rlimits, not
a container: the test still runs as the host user with host network reachable.
Every result produced this way is tagged ``isolation: subprocess`` so reports
make the difference visible.

The environment is built from scratch: PATH, CI=1, HOME=<temp>, plus
PLAYWRIGHT_BROWSERS_PATH (resolved from the real home before scrubbing, else
headless browsers would not be found) and any vars explicitly allowlisted via
$FLAKY_HEALER_SUBPROC_PASS_ENV (e.g. LD_LIBRARY_PATH on hosts where browser
system libraries live in a user-space dir).
"""

from __future__ import annotations

import functools
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

try:  # POSIX-only; imported at module level so the forked child (see
    import resource  # _rlimits) only does a sys.modules lookup — importing
except ImportError:  # between fork and exec in a multi-threaded parent
    resource = None  # (map_runs uses a thread pool) is a deadlock hazard.

from .. import config
from .base import PLAYWRIGHT_ARGS, RunResult, SandboxError, execute_phase, tail

_NPROC_TARGET = 4096

# Post-kill drain budget: after SIGKILLing the session we still communicate()
# to collect output, but a test-spawned daemon that setsid'd away while
# inheriting the stdout pipe would keep the pipe open forever — never block
# the heal on it for more than this.
_KILL_COMMUNICATE_TIMEOUT_S = 10


@functools.lru_cache(maxsize=1)
def _netns_prefix() -> tuple[str, ...]:
    """Best-effort argv prefix that confines the run to a fresh, unprivileged
    user+network namespace: external egress is dropped while loopback stays up,
    so a Playwright webServer on 127.0.0.1 keeps working — closing this
    fallback's biggest gap (the test otherwise reaches the whole host network).

    Returns () when disabled, or when the host can't provide it (no
    unshare/iproute2, user namespaces off) — the heal then degrades to the prior
    host-network behavior rather than failing. Probe-gated on the exact
    capability we need (create namespaces AND bring loopback up); cached because
    it runs once per process, not once per burn-in run.
    """
    if not config.subproc_isolate_net():
        return ()
    unshare = shutil.which("unshare")
    if unshare is None:
        return ()
    probe = [unshare, "--user", "--map-root-user", "--net", "--", "sh", "-c", "ip link set lo up"]
    try:
        if subprocess.run(probe, capture_output=True, timeout=20).returncode != 0:
            return ()
    except (OSError, subprocess.SubprocessError):
        return ()
    # sh runs as root *inside the userns only* (mapped to the real uid outside),
    # which is what lets it bring lo up; Chromium tolerates this. arg0 after the
    # script is a throwaway $0; the real argv follows and is exec'd via "$@".
    return (
        unshare, "--user", "--map-root-user", "--net", "--",
        "sh", "-c", 'ip link set lo up 2>/dev/null || true; exec "$@"', "flaky-healer-netns",
    )


def _rlimits() -> None:  # pragma: no cover - runs in the child process
    # Runs between fork and exec: no imports here (module-level `resource`
    # above is only a globals lookup) and nothing beyond the rlimit calls.
    if resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        cap = _NPROC_TARGET if hard in (resource.RLIM_INFINITY, -1) else min(hard, _NPROC_TARGET)
        if soft == resource.RLIM_INFINITY or soft > cap:
            resource.setrlimit(resource.RLIMIT_NPROC, (cap, hard))
    except (ValueError, OSError):
        pass


class SubprocessSandbox:
    isolation = "subprocess"

    def build_env(self, home: Path | str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "CI": "1",
            "HOME": str(home),
        }
        browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
            Path.home() / ".cache" / "ms-playwright"
        )
        if Path(browsers).is_dir():
            env["PLAYWRIGHT_BROWSERS_PATH"] = browsers
        for name in config.subproc_pass_env():
            if name in os.environ and name not in env:
                env[name] = os.environ[name]
        return env

    def _run_once(self, work: Path, home: Path, test_id: str, timeout_s: int) -> RunResult:
        # `--` after the flags terminates option parsing so a flag-shaped
        # test_id (`--reporter=…`) is treated as a positional spec, not an option.
        cmd = [*_netns_prefix(), "npx", "playwright", "test", *PLAYWRIGHT_ARGS, "--", test_id]
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=work,
                env=self.build_env(home),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                preexec_fn=_rlimits,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            # npx absent or not launchable is an INFRASTRUCTURE failure, not a
            # test failure: reporting it as a RunResult would fabricate
            # "reproduced" verdicts and unfairly charge recipes. Abort the heal.
            raise SandboxError(
                f"subprocess sandbox infrastructure failure: cannot launch 'npx' "
                f"(is Node.js installed and on PATH?): {exc}"
            ) from exc
        try:
            out, _ = proc.communicate(timeout=timeout_s)
            return RunResult(
                exit_code=proc.returncode,
                duration_s=time.monotonic() - start,
                stdout_tail=tail(out or ""),
            )
        except subprocess.TimeoutExpired:
            # kill the whole session: npx, the web server and browser children
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                out, _ = proc.communicate(timeout=_KILL_COMMUNICATE_TIMEOUT_S)
            except subprocess.TimeoutExpired as exc:
                # A daemon escaped the killed session but inherited our stdout
                # pipe: draining would block forever. Close the pipes and return
                # the timed-out result with whatever output was collected.
                out = exc.stdout or ""
                if isinstance(out, bytes):
                    out = out.decode("utf-8", "replace")
                for stream in (proc.stdout, proc.stderr, proc.stdin):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            return RunResult(
                exit_code=-1,
                duration_s=time.monotonic() - start,
                timed_out=True,
                stdout_tail=tail(out or ""),
            )

    def run_test(self, repo_dir, test_id: str, repeats: int = 1, timeout_s: int = 180,
                 prepare=None):
        return execute_phase(
            repo_dir,
            repeats,
            config.run_concurrency(),
            self.isolation,
            prepare,
            lambda work, home: self._run_once(work, home, test_id, timeout_s),
            with_home=True,
        )
