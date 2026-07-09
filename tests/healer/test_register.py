"""Registration contract tests (plan Phase 1 acceptance)."""

from conftest import REPO_ROOT

EXPECTED_TOOLS = {
    "fetch_ci_logs",
    "analyze_playwright_trace",
    "heal_flaky_test",
    "list_healing_recipes",
}
EXPECTED_HOOKS = {"pre_approval_request", "post_approval_response"}


def test_registers_four_tools_with_valid_schemas(fake_ctx, plugin_module):
    plugin_module.register(fake_ctx)
    assert set(fake_ctx.tools) == EXPECTED_TOOLS
    for name, entry in fake_ctx.tools.items():
        assert entry["toolset"] == "flaky_healer"
        schema = entry["schema"]
        assert schema["name"] == name
        assert schema["description"].strip(), f"{name}: description must live inside the schema"
        params = schema["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        for required in params.get("required", []):
            assert required in params["properties"]


def test_registers_two_observer_hooks(fake_ctx, plugin_module):
    plugin_module.register(fake_ctx)
    assert set(fake_ctx.hooks) == EXPECTED_HOOKS
    assert all(len(cbs) == 1 for cbs in fake_ctx.hooks.values())


def test_fetch_ci_logs_gated_by_github_token(fake_ctx, plugin_module, monkeypatch):
    plugin_module.register(fake_ctx)
    check_fn = fake_ctx.tools["fetch_ci_logs"]["check_fn"]
    assert check_fn is not None, "fetch_ci_logs must be env-gated via check_fn"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert check_fn() is False, "tool must be hidden when GITHUB_TOKEN is unset"
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_dummy")
    assert check_fn() is True
    # the other tools must work without GitHub and stay visible
    for other in EXPECTED_TOOLS - {"fetch_ci_logs"}:
        assert fake_ctx.tools[other]["check_fn"] is None


def test_static_skill_registered(fake_ctx, plugin_module):
    plugin_module.register(fake_ctx)
    assert "flaky-healer" in fake_ctx.skills
    assert fake_ctx.skills["flaky-healer"].endswith("SKILL.md")


class _NoSkillHost:
    """Minimal old host: tools + hooks only, no register_skill/register_command."""

    def __init__(self, inner):
        self.inner = inner

    def register_tool(self, *args, **kwargs):
        return self.inner.register_tool(*args, **kwargs)

    def register_hook(self, *args, **kwargs):
        return self.inner.register_hook(*args, **kwargs)


def test_register_survives_host_without_register_skill(fake_ctx, plugin_module):
    plugin_module.register(_NoSkillHost(fake_ctx))  # must not raise
    assert set(fake_ctx.tools) == EXPECTED_TOOLS
    assert set(fake_ctx.hooks) == EXPECTED_HOOKS


def test_manifest_follows_contract():
    # Unified-plugin adaptation: the manifest is the unified plugin's; the
    # healer contract holds within it (tools/hooks listed, no requires_env key
    # — a comment explaining the omission is fine, an actual key is not).
    import re

    manifest = (REPO_ROOT / "plugin.yaml").read_text()
    assert "name: hermes-flaky-stabilization" in manifest
    assert not re.search(r"^requires_env\s*:", manifest, re.M), (
        "GITHUB_TOKEN must NOT be in requires_env — it would disable the whole plugin"
    )
    assert "requires_hermes_version" not in manifest, "field does not exist in PluginManifest"
    for tool in EXPECTED_TOOLS:
        assert tool in manifest
    for hook in EXPECTED_HOOKS:
        assert hook in manifest


def test_skill_file_has_agentskills_frontmatter():
    text = (REPO_ROOT / "skills" / "flaky-healer" / "SKILL.md").read_text()
    assert text.startswith("---\n")
    front = text.split("---", 2)[1]
    assert "name: flaky-healer" in front
    assert "description:" in front


def test_handlers_return_json_strings_and_receive_ctx(fake_ctx, plugin_module):
    import json

    plugin_module.register(fake_ctx)
    handler = fake_ctx.tools["list_healing_recipes"]["handler"]
    out = handler({})
    assert isinstance(out, str), "every tool handler must return a JSON string"
    json.loads(out)
