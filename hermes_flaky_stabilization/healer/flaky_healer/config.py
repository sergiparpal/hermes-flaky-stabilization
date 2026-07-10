"""Environment/config resolution and defaults (stdlib only).

Resolution order for every key that exists in the unified config's ``healer``
section (plan D5): ``FLAKY_HEALER_*`` env var > ``config.json`` ``healer``
section > built-in default. When the unified package is unavailable (flat
legacy import context) or the file is missing/unreadable, behavior degrades
to the historical env-only resolution.
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_NAME = "hermes-flaky-healer"

ENV_DATA_DIR = "FLAKY_HEALER_DATA_DIR"
ENV_BURNIN = "FLAKY_HEALER_BURNIN"
ENV_SANDBOX = "FLAKY_HEALER_SANDBOX"
ENV_DOCKER_IMAGE = "FLAKY_HEALER_DOCKER_IMAGE"
ENV_GIT_TOOL = "FLAKY_HEALER_GIT_TOOL"
ENV_PR_TOOL = "FLAKY_HEALER_PR_TOOL"
ENV_BASE_BRANCH = "FLAKY_HEALER_BASE_BRANCH"
ENV_SUBPROC_PASS = "FLAKY_HEALER_SUBPROC_PASS_ENV"
ENV_GITHUB_API = "FLAKY_HEALER_GITHUB_API"
ENV_ALLOW_SUBPROC_PR = "FLAKY_HEALER_ALLOW_SUBPROCESS_PR"
ENV_SUBPROC_ISOLATE_NET = "FLAKY_HEALER_SUBPROC_ISOLATE_NET"
ENV_RUN_CONCURRENCY = "FLAKY_HEALER_RUN_CONCURRENCY"
ENV_HARDLINK_COPY = "FLAKY_HEALER_HARDLINK_COPY"

# Pinned to the @playwright/test version in fixtures/toy-app/package.json so the
# image's bundled browsers match the project's playwright-core revision, AND by
# content digest so a moved/compromised tag cannot swap the image under us
# (the tag stays in the ref for readability; the @sha256 is what docker verifies).
# Override with FLAKY_HEALER_DOCKER_IMAGE to pin a different digest.
DEFAULT_DOCKER_IMAGE = (
    "mcr.microsoft.com/playwright:v1.60.0-noble"
    "@sha256:9bd26ad900bb5e0f4dee75839e957a89ae89c2b7ab1e76050e559790e946b948"
)
DEFAULT_BURNIN = (5, 10)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _unified_healer_config() -> dict:
    """The ``healer`` section of the unified flaky-stabilization config.json.

    The unified package resolves ``<hermes_home>`` itself via its ``paths``
    helper (registration binds ``ctx.hermes_home`` to the same profile; this
    module stays ctx-free by design). Imported lazily to avoid a circular
    import at package init, and every failure — package not importable,
    unreadable/absent file — degrades to ``{}`` so env-only behavior is
    preserved exactly when the section is unset.
    """
    try:
        from hermes_flaky_stabilization import config as unified_config
        from hermes_flaky_stabilization import paths as unified_paths
    except Exception:  # pragma: no cover — flat import context without the package
        return {}
    try:
        # get_hermes_home()/<dir> rather than get_data_dir(): reading config
        # must not create the data directory as a side effect.
        section_dir = unified_paths.get_hermes_home() / unified_paths.DATA_DIR_NAME
        section = unified_config.load_config(section_dir).get("healer")
    except Exception:  # noqa: BLE001 — config lookup must never break the healer
        return {}
    return section if isinstance(section, dict) else {}


def _cfg_str(key: str) -> str | None:
    """A non-empty string value for *key* from the unified section, else None."""
    value = _unified_healer_config().get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def data_dir(ctx=None) -> Path:
    """Profile-aware persistent data dir, created on demand.

    Unified-plugin adaptation (plan Phase 4 / §10): the default moved from
    ``plugins-data/hermes-flaky-healer/`` to the unified
    ``<hermes_home>/flaky-stabilization/`` directory (patches under
    ``patches/``, the ``learned-patterns.md`` mirror beside them). The
    ``$FLAKY_HEALER_DATA_DIR`` override keeps working and preserves any legacy
    location. Resolution order: $FLAKY_HEALER_DATA_DIR → ctx.hermes_home →
    $HERMES_HOME → ~/.hermes, then ``/flaky-stabilization``.
    """
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        base = Path(env)
    else:
        hermes_home = getattr(ctx, "hermes_home", None) or os.environ.get("HERMES_HOME")
        root = Path(hermes_home) if hermes_home else Path.home() / ".hermes"
        base = root / "flaky-stabilization"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _parse_burnin(raw: str) -> tuple[int, int] | None:
    try:
        m, n = raw.split(":")
        return max(1, int(m)), max(1, int(n))
    except ValueError:
        return None


def burnin() -> tuple[int, int]:
    """(reproduce_runs M, burn_in_runs N); FLAKY_HEALER_BURNIN='5:10' > config."""
    for raw in (os.environ.get(ENV_BURNIN, ""), _cfg_str("burnin") or ""):
        if raw:
            parsed = _parse_burnin(raw)
            if parsed is not None:
                return parsed
    return DEFAULT_BURNIN


def github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or None


def github_api_base() -> str:
    return os.environ.get(ENV_GITHUB_API, "https://api.github.com").rstrip("/")


def sandbox_backend() -> str:
    """'auto' | 'docker' | 'subprocess'."""
    env = (os.environ.get(ENV_SANDBOX) or "").strip().lower()
    return env or (_cfg_str("sandbox") or "auto").lower()


def docker_image() -> str:
    return os.environ.get(ENV_DOCKER_IMAGE) or _cfg_str("docker_image") or DEFAULT_DOCKER_IMAGE


def git_tool() -> str:
    """Host tool name used to run git commands through the approval pipeline."""
    return os.environ.get(ENV_GIT_TOOL) or _cfg_str("git_tool") or "terminal"


def pr_tool() -> str:
    """Host tool name used to open a pull request through the approval pipeline."""
    return os.environ.get(ENV_PR_TOOL) or _cfg_str("pr_tool") or "create_pull_request"


def base_branch() -> str:
    return os.environ.get(ENV_BASE_BRANCH) or _cfg_str("base_branch") or "main"


def subproc_pass_env() -> list[str]:
    """Extra env var names the subprocess sandbox may copy from the host env.

    The subprocess backend builds its environment from scratch; this explicit
    allowlist exists for hosts where browsers need e.g. LD_LIBRARY_PATH.
    """
    raw = os.environ.get(ENV_SUBPROC_PASS, "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def allow_subprocess_pr() -> bool:
    """Whether mode='pr' may proceed on a result validated in the WEAKER
    subprocess sandbox. Off by default: a subprocess heal ran (and any injected
    patch executed) on the host with no container isolation, so it must not be
    auto-promoted to a PR unless the operator explicitly opts in.
    """
    raw = os.environ.get(ENV_ALLOW_SUBPROC_PR)
    if raw is not None and raw.strip():
        return _truthy(raw)
    value = _unified_healer_config().get("allow_subprocess_pr")
    return value if isinstance(value, bool) else False


def subproc_isolate_net() -> bool:
    """Whether the subprocess sandbox should drop external network via an
    unprivileged user+network namespace (loopback stays up for the Playwright
    webServer). Best-effort and probe-gated; on by default, set to 0/false to
    disable on hosts whose tests legitimately reach external services.
    """
    return _truthy(os.environ.get(ENV_SUBPROC_ISOLATE_NET, "1"))


def run_concurrency() -> int:
    """Max test runs to execute in parallel within one reproduce/burn-in phase.

    Default 1 (fully sequential — today's behavior). >1 runs the repeats
    concurrently, each in its OWN isolated project copy. Trade-offs the operator
    must size for:

    * Docker: every container still reserves ``--memory=2g``/``--cpus=2``, so N
      parallel runs need N×2 GiB RAM and 2N CPUs.
    * Subprocess: parallel runs rely on per-run network-namespace isolation
      (``FLAKY_HEALER_SUBPROC_ISOLATE_NET``) for a private loopback; with net
      isolation off/unavailable they would collide on the webServer port, so
      keep this at 1 there.
    """
    try:
        return max(1, int(os.environ.get(ENV_RUN_CONCURRENCY, "1")))
    except ValueError:
        return 1


def hardlink_copy() -> bool:
    """Whether ``copy_project`` may hardlink files instead of byte-copying them.

    Off by default. When on, project copies (above all the large, immutable
    ``node_modules`` tree) are hardlinked on the same filesystem — near-instant
    and disk-free — with a per-file real-copy fallback across devices. Safe
    because ``apply_ops`` replaces patched files via a sibling temp file +
    ``os.replace``, landing every write on a NEW inode instead of the
    hardlinked original (writing through the shared inode would silently edit
    the user's real project); ``node_modules`` is never mutated at run time.
    Enable on hosts where the project tree lives on a single filesystem.
    """
    return _truthy(os.environ.get(ENV_HARDLINK_COPY))
