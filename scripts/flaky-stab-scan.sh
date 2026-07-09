#!/usr/bin/env bash
#
# Nightly no-agent cron shim for hermes-flaky-stabilization.
#
# Installed into ~/.hermes/scripts/ by `hermes flaky-stab install-cron`.
# A no-agent cron job runs this on a schedule and delivers its stdout verbatim at
# zero LLM cost. `--format cron` prints the flaky-test changes (or nothing, for a
# silent tick on quiet nights). A non-zero exit makes Hermes deliver an error
# alert, so a broken sweep cannot fail silently: `exec` replaces this shell with
# the scan process, so the scan's own exit code becomes the job's exit code
# directly. `set -euo pipefail` guards the lines before the exec (and any future
# non-exec edits) against unset vars and silent failures.
set -euo pipefail
exec hermes flaky-stab scan --format cron
