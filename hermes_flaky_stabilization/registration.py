"""All Hermes-facing wiring: tools, hooks, commands, skills, CLI.

Only this module and the package ``__init__`` may import Hermes surfaces;
stage packages receive ``llm`` / ``dispatch_tool`` / paths as injected
parameters (plan D3). Registrations grow phase by phase; each stage's wiring
lands with its port.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PLUGIN_NAME = "hermes-flaky-stabilization"
CLI_COMMAND = "flaky-stab"


def register(ctx) -> None:
    """Wire the plugin surface onto *ctx* (a Hermes ``PluginContext``)."""
    from . import bugreport, detective, healer, history, incidents, pii, triage

    # Stage registrations (tool contracts preserved verbatim). history.register
    # also registers the `test-history` CLI alias — a kept public contract (D2);
    # bugreport.register also registers the /improve-bug slash command;
    # healer.register also registers /heal, the flaky-healer skill, and the two
    # approval audit observer hooks.
    history.register(ctx)
    detective.register(ctx)
    triage.register(ctx)
    healer.register(ctx)
    bugreport.register(ctx)
    pii.register(ctx)
    incidents_service = incidents.register(ctx)

    # NET-NEW tracker write-back (plan D7) — shipped disabled, check_fn-hidden.
    from .incidents import write as incidents_write

    incidents_write.register_tool(ctx)

    # NET-NEW orchestration surface (plan D9).
    from .orchestrator import pipeline as orchestrator_pipeline

    ctx.register_tool(
        name="stabilize_test_failure",
        toolset="flaky_stabilization",
        schema=orchestrator_pipeline.STABILIZE_SCHEMA,
        handler=orchestrator_pipeline.make_tool_handler(ctx, incidents_service),
    )
    ctx.register_tool(
        name="find_duplicate_incidents",
        toolset="flaky_stabilization",
        schema=orchestrator_pipeline.FIND_DUPLICATES_SCHEMA,
        handler=orchestrator_pipeline.make_find_duplicates_handler(incidents_service),
    )
    try:
        ctx.register_command(
            "stabilize",
            orchestrator_pipeline.make_command(ctx, incidents_service),
            description=(
                "Run the stabilization pipeline on a CI log or failing test: "
                "/stabilize <log-or-test-id> [repo_dir=…] [mode=suggest|pr]"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — command is optional
        logger.warning("%s: /stabilize not registered: %s", PLUGIN_NAME, exc)

    # Lifecycle + gating hooks (incident context injection, session-start sync,
    # approval escalation).
    from .orchestrator import hooks as orchestrator_hooks

    orchestrator_hooks.register(ctx, incidents_service=incidents_service)

    # The triage pattern store connects lazily inside tool calls; resolving the
    # data dir here guarantees its 0700 permissions exist before any handler
    # opens state.db through the injected-home path.
    from . import paths

    paths.get_data_dir()

    _register_cli(ctx)


def _register_cli(ctx) -> None:
    from . import cli

    try:
        ctx.register_cli_command(
            name=CLI_COMMAND,
            help="Unified flaky-test stabilization pipeline (history, detection, triage, healing)",
            setup_fn=cli.setup_cli,
            handler_fn=cli.run_cli,
            description=(
                "Manage the hermes-flaky-stabilization plugin: ingest JUnit XML, scan for "
                "flaky tests, sync the Jira incident index, migrate legacy plugin data, "
                "and install the nightly cron job."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — a CLI mismatch must not block tool loading
        logger.warning("%s: CLI command %r not registered: %s", PLUGIN_NAME, CLI_COMMAND, exc)
