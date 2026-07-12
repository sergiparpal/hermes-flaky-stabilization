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
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX
    resource = None

from .. import config
from .base import PLAYWRIGHT_ARGS, RunResult, SandboxError, execute_phase, tail

_OUTPUT_TAIL_BYTES = 64 * 1024

# Post-kill drain budget: after SIGKILLing the session we still communicate()
# to collect output, but a test-spawned daemon that setsid'd away while
# inheriting the stdout pipe would keep the pipe open forever — never block
# the heal on it for more than this.
_KILL_COMMUNICATE_TIMEOUT_S = 10


def _rlimits() -> None:  # pragma: no cover - retained for embedders; not used by Popen
    """Apply basic limits when an embedding host invokes this in a safe child."""
    if resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass


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


def _bounded_wait(proc: subprocess.Popen, timeout_s: int) -> tuple[bytes, bool]:
    """Drain output without unbounded buffering; return tail and timeout flag."""
    assert proc.stdout is not None
    try:
        fd = proc.stdout.fileno()
    except (AttributeError, OSError):
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or b""
            if isinstance(out, str):
                out = out.encode("utf-8", "replace")
            return out[-_OUTPUT_TAIL_BYTES:], True
        if isinstance(out, str):
            out = out.encode("utf-8", "replace")
        return out[-_OUTPUT_TAIL_BYTES:], False
    os.set_blocking(fd, False)
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout_s
    timed_out = False
    try:
        while proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for _key, _mask in selector.select(min(0.2, remaining)):
                try:
                    chunk = os.read(fd, 8192)
                except BlockingIOError:
                    continue
                output.extend(chunk)
                if len(output) > _OUTPUT_TAIL_BYTES:
                    del output[:-_OUTPUT_TAIL_BYTES]
        # Drain bytes already in the pipe, but never wait for an escaped child
        # that inherited stdout after the runner itself exited.
        while selector.select(0):
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                break
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _OUTPUT_TAIL_BYTES:
                del output[:-_OUTPUT_TAIL_BYTES]
    finally:
        selector.close()
    return bytes(output), timed_out


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
        out, timed_out = _bounded_wait(proc, timeout_s)
        if not timed_out:
            return RunResult(
                exit_code=proc.returncode,
                duration_s=time.monotonic() - start,
                stdout_tail=tail(out.decode("utf-8", "replace")),
            )
        # kill the whole session: npx, the web server and browser children
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.wait(timeout=_KILL_COMMUNICATE_TIMEOUT_S)
        except (AttributeError, subprocess.TimeoutExpired):
            pass
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        return RunResult(
            exit_code=-1,
            duration_s=time.monotonic() - start,
            timed_out=True,
            stdout_tail=tail(out.decode("utf-8", "replace")),
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
