"""Sandbox protocol, run reports, project copying and the backend factory."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import config

# Always force deterministic single runs inside the sandbox regardless of the
# project's own playwright config.
PLAYWRIGHT_ARGS = ("--reporter=line", "--workers=1", "--retries=0", "--trace=off")

# Name-based excludes for project copies: VCS, prior artifacts, caches.
COPY_EXCLUDES = {
    ".git",
    "test-results",
    "playwright-report",
    ".cache",
    "results.json",
    "run-report.json",
}


class SandboxError(RuntimeError):
    pass


@dataclass
class RunResult:
    exit_code: int
    duration_s: float
    timed_out: bool = False
    stdout_tail: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass
class RunReport:
    runs: int
    passed: int
    failed: int
    isolation: str
    duration_s: float
    results: list[RunResult] = field(default_factory=list)

    @property
    def all_green(self) -> bool:
        return self.runs > 0 and self.failed == 0

    @property
    def any_failed(self) -> bool:
        return self.failed > 0

    def to_dict(self) -> dict:
        return {
            "runs": self.runs,
            "passed": self.passed,
            "failed": self.failed,
            "isolation": self.isolation,
            "duration_s": round(self.duration_s, 2),
            "exit_codes": [r.exit_code for r in self.results],
            "timed_out_runs": sum(1 for r in self.results if r.timed_out),
            "last_stdout_tail": self.results[-1].stdout_tail if self.results else "",
        }


def aggregate(results: list[RunResult], isolation: str) -> RunReport:
    return RunReport(
        runs=len(results),
        passed=sum(1 for r in results if r.passed),
        failed=sum(1 for r in results if not r.passed),
        isolation=isolation,
        duration_s=sum(r.duration_s for r in results),
        results=results,
    )


def _copy_ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in COPY_EXCLUDES}


def _hardlink_or_copy(src: str, dst: str) -> None:
    """copytree copy_function that hardlinks regular files, falling back to a
    real copy across devices (EXDEV) or for anything os.link can't link."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def copy_project(src: Path | str, dst: Path | str) -> Path:
    """Copy a Playwright project (incl. node_modules, excl. artifacts/VCS).

    Byte-copies by default; hardlinks files when ``FLAKY_HEALER_HARDLINK_COPY``
    is set (see ``config.hardlink_copy`` for the safety rationale).
    """
    src, dst = Path(src), Path(dst)
    if not src.is_dir():
        raise SandboxError(f"project directory does not exist: {src}")
    copy_function = _hardlink_or_copy if config.hardlink_copy() else shutil.copy2
    shutil.copytree(src, dst, symlinks=True, ignore=_copy_ignore, copy_function=copy_function)
    return dst


def map_runs(thunks: list[Callable[[], RunResult]], concurrency: int) -> list[RunResult]:
    """Execute zero-arg run thunks, preserving order.

    Serial when ``concurrency<=1`` (or a single thunk); otherwise bounded-
    concurrent via a thread pool — each thunk's work is a blocking subprocess
    that releases the GIL, so threads give real parallelism. Exceptions from a
    thunk propagate (the pool still joins all started threads on exit).
    """
    if concurrency <= 1 or len(thunks) <= 1:
        return [thunk() for thunk in thunks]
    results: list = [None] * len(thunks)
    with ThreadPoolExecutor(max_workers=min(concurrency, len(thunks))) as pool:
        futures = {pool.submit(thunk): i for i, thunk in enumerate(thunks)}
        for future in futures:
            results[futures[future]] = future.result()
    return results


def execute_phase(
    repo_dir: Path | str,
    repeats: int,
    concurrency: int,
    isolation: str,
    prepare: Callable[[Path], None] | None,
    run_one: Callable[[Path, Path | None], RunResult],
    *,
    with_home: bool = False,
) -> RunReport:
    """Run one reproduce/burn-in phase, shared by both sandbox backends.

    Serial (``concurrency<=1``): make ONE project copy, optionally ``prepare`` it
    (apply the patch in place), then ``run_one`` it ``repeats`` times — the
    single-copy fast path. Parallel: give each run its OWN copy (so concurrent
    runs never collide on test-results/ or the webServer port) and execute up to
    ``concurrency`` at once. ``with_home`` provisions a per-run HOME dir (the
    subprocess backend needs one); ``run_one`` receives ``(work, home)``.
    """
    prefix = f"flaky-healer-{isolation}-"

    def _workspace(tmp: str) -> tuple[Path, Path | None]:
        work = copy_project(repo_dir, Path(tmp) / "project")
        home: Path | None = None
        if with_home:
            home = Path(tmp) / "home"
            home.mkdir()
        if prepare is not None:
            prepare(work)
        return work, home

    if concurrency <= 1 or repeats <= 1:
        with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
            work, home = _workspace(tmp)
            results = [run_one(work, home) for _ in range(repeats)]
        return aggregate(results, isolation)

    def _isolated_run() -> RunResult:
        with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
            work, home = _workspace(tmp)
            return run_one(work, home)

    return aggregate(map_runs([_isolated_run] * repeats, concurrency), isolation)


def tail(text: str, max_bytes: int = 4000) -> str:
    return text[-max_bytes:] if text else ""


@runtime_checkable
class Sandbox(Protocol):
    isolation: str

    def run_test(
        self,
        repo_dir: Path | str,
        test_id: str,
        repeats: int = 1,
        timeout_s: int = 180,
        prepare: Callable[[Path], None] | None = None,
    ) -> RunReport:
        """Run ``test_id`` ``repeats`` times in an isolated copy of ``repo_dir``.

        ``prepare(work)`` is invoked on each project copy after it is made and
        before the runs — the heal pipeline uses it to apply the patch in place,
        so the patched burn-in needs no separate scratch copy.
        """
        ...


def docker_available(docker_bin: str = "docker") -> bool:
    if shutil.which(docker_bin) is None:
        return False
    try:
        proc = subprocess.run(
            [docker_bin, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def create_sandbox(prefer: str | None = None) -> Sandbox:
    """Backend factory honoring DP-1: docker when reachable, else subprocess.

    ``prefer`` (or $FLAKY_HEALER_SANDBOX) ∈ {auto, docker, subprocess}.
    """
    from .docker import DockerSandbox
    from .subproc import SubprocessSandbox

    choice = (prefer or config.sandbox_backend()).lower()
    if choice == "docker":
        if not docker_available():
            raise SandboxError("FLAKY_HEALER_SANDBOX=docker but no reachable Docker daemon")
        return DockerSandbox()
    if choice == "subprocess":
        return SubprocessSandbox()
    if choice != "auto":
        raise SandboxError(f"unknown sandbox backend {choice!r} (auto|docker|subprocess)")
    return DockerSandbox() if docker_available() else SubprocessSandbox()
