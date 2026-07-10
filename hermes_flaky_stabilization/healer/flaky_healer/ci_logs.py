"""Pure CI run-log helpers: fetch-adapter construction, zip decoding, scrubbing.

Split out of ``handlers`` (which was carrying six unrelated responsibilities):
this module owns the CI-log mechanics — building the GitHub Actions adapter,
turning a run-logs zip into text under the zip-bomb budget, and scrubbing
credentials from it — while the thin ``fetch_ci_logs`` / ``heal_flaky_test`` tool
handlers stay in ``handlers`` and call these. No Hermes/host dependency.
"""

from __future__ import annotations

import io
import zipfile
import zlib

from . import config, zipsafe
from . import redact as redact_mod
from .ci import github_actions

# A GitHub Actions job/step with any of these conclusions is NOT a failure; the
# CI-log fetch keeps only jobs and steps whose conclusion is outside this set.
NON_FAILURE_CONCLUSIONS = (None, "success", "skipped", "neutral")

# zip entry reads can raise zlib.error/RuntimeError on corrupt/encrypted members,
# which the BadZipFile guard alone does not cover; ZipLimitError guards bombs.
ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    RuntimeError,
    OSError,
    zipsafe.ZipLimitError,
)


def scrub_ci_secrets(text: str) -> str:
    """Redact credentials from CI-log text before it reaches the model.

    ``logfilter.filter_log`` is a failure-anchor prefilter, not a redactor, and
    CI logs cluster secrets (``set -x``, env dumps, ``curl -H "Authorization:
    …"``) exactly at/around the failures it keeps. The triage pipeline redacts
    log excerpts at its chokepoint; the healer's ``fetch_ci_logs`` /
    ``heal_flaky_test`` diagnosis paths must do the same rather than hand raw
    secrets to the LLM. Use the shared secret scrubber (the same shapes triage
    uses — no cross-stage import); fall back to the healer's own token-shape
    mask if the kernel cannot be imported."""
    if not text:
        return text
    try:
        from hermes_flaky_stabilization.common import secretscrub
    except Exception:  # pragma: no cover - flat/path-loaded import fallback
        return redact_mod.mask_tokens(text)
    return secretscrub.scrub_text(text)


def logs_zip_to_text(blob: bytes, failed_job_names: set[str] | None = None) -> str:
    """Concatenate the .txt entries of a GH run-logs zip, one header per entry.

    When failed job names are known, only their per-job folders are read; if
    that filter matches nothing (layout drift), fall back to every entry.
    """
    zf = zipfile.ZipFile(io.BytesIO(blob))
    # The run-logs archive is only semi-trusted (a malicious workflow controls
    # its contents/size); bound entry count and decompressed bytes like traces.
    zipsafe.guard_entry_count(zf)
    budget = zipsafe.ZipBudget()
    names = [n for n in zf.namelist() if n.endswith(".txt")]

    def folder(name: str) -> str:
        return name.split("/", 1)[0] if "/" in name else ""

    selected = names
    if failed_job_names:
        filtered = [n for n in names if folder(n) in failed_job_names]
        if filtered:
            selected = filtered
    parts = []
    for name in sorted(selected):
        body = budget.read(zf, name).decode("utf-8", "replace")
        parts.append(f"===== {name} =====\n{body}")
    return "\n".join(parts)


def make_ci_adapter(transport):
    """Build the GitHub Actions CI adapter shared by the two CI-log call sites.

    Returns ``(adapter, has_credentials)``. ``has_credentials`` is False ONLY in
    the "no GITHUB_TOKEN and no injected transport" case — the sole case the
    adapter cannot be built to reach the API, so ``adapter`` is then ``None``.
    The two callers diverge on that signal (``fetch_ci_logs`` returns an error
    envelope, ``_pull_ci_log`` degrades to a warning), so the helper only
    centralizes the identical token guard, the ``"injected-transport"`` token
    fallback and the ``api_base`` wiring, keeping the two sites from drifting.
    """
    token = config.github_token()
    if not token and transport is None:
        return None, False
    adapter = github_actions.GitHubActionsCI(
        token=token or "injected-transport",
        transport=transport,
        api_base=config.github_api_base(),
    )
    return adapter, True
