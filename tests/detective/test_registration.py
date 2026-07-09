"""Plugin registration wires the is_flaky tool + CLI command, and the handler
returns the documented JSON shapes.

No live Hermes needed: a ``FakeCtx`` records what ``register`` would have
registered, and the handler runs against a seeded temp verdicts DB.
"""

import json

from hermes_flaky_detective import (
    IS_FLAKY_SCHEMA,
    _handle_is_flaky,
    detect,
    domain,
    register,
    storage,
)


class FakeCtx:
    """Minimal stand-in for Hermes's PluginContext that records registrations."""

    def __init__(self):
        self.tools = {}
        self.cli_commands = {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kwargs}

    def register_cli_command(self, name, help, setup_fn, handler_fn, **kwargs):
        self.cli_commands[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}


def _v(test_key, status, *, passes, fails, classname="pkg.Mod", name=None, file_path="src/mod.py"):
    name = name or test_key.split("::")[-1]
    return detect.Verdict(
        test_key=test_key, classname=classname, name=name, file_path=file_path,
        passes=passes, fails=fails, runs=passes + fails, window_days=14,
        first_seen="2026-05-20T09:00:00", last_seen="2026-05-25T09:00:00",
        last_failure="2026-05-24T09:00:00" if fails else None, status=status,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_wires_tool_in_flaky_detective_toolset():
    ctx = FakeCtx()
    register(ctx)
    assert set(ctx.tools) == {"is_flaky"}
    assert ctx.tools["is_flaky"]["toolset"] == "flaky_detective"
    assert callable(ctx.tools["is_flaky"]["handler"])


def test_register_wires_no_cli_command():
    # Unified-plugin adaptation (plan Appendix C): the legacy `flaky-detective`
    # CLI is re-homed under the top-level `flaky-stab` command, wired by the
    # unified registration — the stage register() must not claim a CLI name.
    ctx = FakeCtx()
    register(ctx)
    assert ctx.cli_commands == {}


def test_schema_is_valid_and_serializable():
    assert IS_FLAKY_SCHEMA["name"] == "is_flaky"
    assert IS_FLAKY_SCHEMA["parameters"]["required"] == ["test_id"]
    json.dumps(IS_FLAKY_SCHEMA)   # must be JSON-serializable to reach the model


# ---------------------------------------------------------------------------
# Handler JSON shapes (against a seeded temp DB)
# ---------------------------------------------------------------------------


def _seed(store):
    store.replace_verdicts([
        _v("auth.test_oauth::test_oauth_login", domain.VERDICT_FLAKY, passes=4, fails=3,
           classname="auth.test_oauth", file_path="src/auth/test_oauth.py"),
        _v("billing.test_pay::test_charge", domain.VERDICT_STABLE, passes=10, fails=0,
           classname="billing.test_pay", file_path="src/billing/test_pay.py"),
    ])


def test_handler_returns_flaky_verdict(profile_env):
    with storage.Storage() as store:
        _seed(store)
        payload = json.loads(_handle_is_flaky(
            {"test_id": "auth.test_oauth::test_oauth_login"}, store=store))
    assert payload["success"] is True
    assert payload["is_flaky"] is True
    assert payload["status"] == domain.VERDICT_FLAKY
    assert (payload["fails"], payload["passes"], payload["runs"]) == (3, 4, 7)
    assert payload["window_days"] == 14
    assert payload["last_failure"] == "2026-05-24T09:00:00"
    assert "computed_at" in payload
    assert "content_warning" in payload    # untrusted-data notice always present


def test_handler_returns_non_flaky_for_stable(profile_env):
    with storage.Storage() as store:
        _seed(store)
        payload = json.loads(_handle_is_flaky({"test_id": "test_charge"}, store=store))  # bare name
    assert payload["success"] is True
    assert payload["is_flaky"] is False
    assert payload["status"] == domain.VERDICT_STABLE


def test_handler_resolves_by_file_path_and_name(profile_env):
    with storage.Storage() as store:
        _seed(store)
        payload = json.loads(_handle_is_flaky(
            {"test_id": "src/auth/test_oauth.py::test_oauth_login"}, store=store))
    assert payload["success"] is True
    assert payload["is_flaky"] is True
    assert payload["test_key"] == "auth.test_oauth::test_oauth_login"


def test_handler_unknown_for_unseen_test(profile_env):
    with storage.Storage() as store:
        _seed(store)
        payload = json.loads(_handle_is_flaky({"test_id": "nonexistent_test"}, store=store))
    assert payload["success"] is True
    assert payload["is_flaky"] is False
    assert payload["status"] == domain.VERDICT_UNKNOWN
    assert "note" in payload


def test_handler_unknown_when_never_scanned(profile_env):
    # No seed: the DB exists (schema applied) but has zero verdicts.
    with storage.Storage() as store:
        payload = json.loads(_handle_is_flaky({"test_id": "anything"}, store=store))
    assert payload["success"] is True
    assert payload["status"] == domain.VERDICT_UNKNOWN


def test_handler_bad_input(profile_env):
    for bad in ({"test_id": ""}, {"test_id": None}, {}):
        payload = json.loads(_handle_is_flaky(bad))
        assert payload["success"] is False
        assert "error" in payload and "remediation" in payload


def test_handler_non_dict_params_returns_error_json(profile_env):
    # The contract is "always return a JSON string"; a non-dict params (e.g. None)
    # must become an error response, not raise AttributeError on params.get().
    for bad in (None, "test_a", 42, ["test_a"]):
        payload = json.loads(_handle_is_flaky(bad))
        assert payload["success"] is False
        assert "error" in payload and "remediation" in payload


def test_handler_returns_json_string(profile_env):
    # No store passed: the handler opens (and closes) a transient Storage itself.
    assert isinstance(_handle_is_flaky({"test_id": "x"}), str)
