"""Docker sandbox: pinned Playwright image, no network, capped resources.

Isolation requirements (plan §4.5): ``--network=none --memory=2g --cpus=2
--pids-limit=256 --user <uid>:<gid>``, a temp copy of the project bind-mounted
at /work, and an empty environment except PATH (the image's own), CI=1 and
HOME=/tmp. The project's node_modules travels with the copy — nothing is
installed inside the container because it has no network.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from .. import config
from .base import (
    PLAYWRIGHT_ARGS,
    RunResult,
    SandboxError,
    execute_phase,
    tail,
)
from .subproc import _bounded_wait

# docker-CLI exit codes that mean the CONTAINER NEVER RAN the test command:
# 125 = the docker daemon/CLI itself errored, 126 = the command cannot be
# invoked, 127 = the command was not found. Recording these as ordinary test
# failures would fabricate "reproduced" verdicts and unfairly charge recipes,
# so they abort the heal as infrastructure errors instead.
_DOCKER_INFRA_EXIT_CODES = frozenset({125, 126, 127})


class DockerSandbox:
    isolation = "docker"

    def __init__(self, image: str | None = None, docker_bin: str = "docker"):
        self.image = image or config.docker_image()
        self.docker_bin = docker_bin
        self._image_checked = False

    # -- command construction (unit-testable without a daemon) ---------------

    def build_command(self, work_dir: Path | str, test_id: str, name: str) -> list[str]:
        cmd = [
            self.docker_bin,
            "run",
            "--rm",
            f"--name={name}",
            "--network=none",
            "--memory=2g",
            "--cpus=2",
            "--pids-limit=256",
            # Minimize the blast radius if test code escapes the runner: drop all
            # Linux capabilities, forbid privilege escalation, and make the root
            # filesystem read-only. The bind-mounted /work stays writable for test
            # artifacts; /tmp (HOME) is a sized tmpfs so the runner can still write.
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--tmpfs=/tmp:rw,exec,size=512m",
        ]
        # --user uid:gid is POSIX-only; on hosts without getuid (e.g. Windows)
        # let the container run as its default user rather than crashing.
        if hasattr(os, "getuid") and hasattr(os, "getgid"):
            cmd.append(f"--user={os.getuid()}:{os.getgid()}")
        cmd += [
            "-e",
            "CI=1",
            "-e",
            "HOME=/tmp",
            "-v",
            f"{Path(work_dir).resolve()}:/work",
            "-w",
            "/work",
            self.image,
            "npx",
            "playwright",
            "test",
            *PLAYWRIGHT_ARGS,
            # `--` terminates option parsing (all our flags precede it) so a
            # test_id shaped like a flag (e.g. `--reporter=…`) is treated as a
            # positional spec, not an option. test_id is already confined to an
            # existing repo file by the healer, so this is belt-and-suspenders.
            "--",
            test_id,
        ]
        return cmd

    # -- execution ------------------------------------------------------------

    def _ensure_image(self) -> None:
        """Pull the pinned image once if absent (daemon-side network; the test
        containers themselves still run with --network=none)."""
        if self._image_checked:
            return
        try:
            inspect = subprocess.run(
                [self.docker_bin, "image", "inspect", self.image],
                capture_output=True,
                timeout=30,
            )
            if inspect.returncode != 0:
                pull = subprocess.run(
                    [self.docker_bin, "pull", self.image],
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
                if pull.returncode != 0:
                    raise SandboxError(
                        f"cannot pull sandbox image {self.image}: {tail(pull.stderr, 500)}"
                    )
        except FileNotFoundError as exc:
            raise SandboxError(f"docker binary {self.docker_bin!r} not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"timed out preparing sandbox image {self.image}: {exc}") from exc
        self._image_checked = True

    def _run_once(self, work_dir: Path, test_id: str, timeout_s: int) -> RunResult:
        name = f"flaky-healer-{uuid.uuid4().hex[:12]}"
        cmd = self.build_command(work_dir, test_id, name)
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            output, timed_out = _bounded_wait(proc, timeout_s)
            text = output.decode("utf-8", "replace")
            if timed_out:
                try:
                    subprocess.run(
                        [self.docker_bin, "kill", name], capture_output=True, timeout=30
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
                return RunResult(
                    exit_code=-1,
                    duration_s=time.monotonic() - start,
                    timed_out=True,
                    stdout_tail=tail(text),
                )
            if proc.returncode in _DOCKER_INFRA_EXIT_CODES:
                raise SandboxError(
                    "docker sandbox infrastructure failure (docker exited "
                    f"{proc.returncode}; the test never ran): "
                    f"{tail(text, 500)}"
                )
            return RunResult(
                exit_code=proc.returncode,
                duration_s=time.monotonic() - start,
                stdout_tail=tail(text),
            )
        except OSError as exc:
            raise SandboxError(f"cannot launch docker sandbox: {exc}") from exc

    def run_test(self, repo_dir, test_id: str, repeats: int = 1, timeout_s: int = 180,
                 prepare=None):
        self._ensure_image()
        return execute_phase(
            repo_dir,
            repeats,
            config.run_concurrency(),
            self.isolation,
            prepare,
            lambda work, _home: self._run_once(work, test_id, timeout_s),
        )
