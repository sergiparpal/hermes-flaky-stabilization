"""Unified config load/merge/coerce (plan Appendix B)."""

from __future__ import annotations

import copy
import json
import stat

from hermes_flaky_stabilization import config


def test_missing_file_yields_defaults(profile_env):
    assert config.load_config() == config.DEFAULTS


def test_defaults_are_not_mutated_by_load(profile_env):
    snapshot = copy.deepcopy(config.DEFAULTS)
    cfg = config.load_config()
    cfg["detective"]["window_days"] = 999
    cfg["pipeline"]["heal_categories"].append("mutated")
    assert config.DEFAULTS == snapshot


def test_partial_file_merges_over_defaults(profile_env):
    config.write_config({"detective": {"window_days": 7}})
    cfg = config.load_config()
    assert cfg["detective"]["window_days"] == 7
    assert cfg["detective"]["min_fails"] == 3  # untouched default
    assert cfg["jira"]["enable_write"] is False


def test_malformed_file_degrades_to_defaults(profile_env):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert config.load_config() == config.DEFAULTS


def test_wrong_typed_value_falls_back_to_default(profile_env):
    config.write_config(
        {
            "detective": {"window_days": "fourteen", "include_errors": "yes"},
            "jira": {"enable_write": 1},
        }
    )
    cfg = config.load_config()
    assert cfg["detective"]["window_days"] == 14
    assert cfg["detective"]["include_errors"] is True
    assert cfg["jira"]["enable_write"] is False  # int 1 is not bool True


def test_unknown_sections_and_keys_preserved(profile_env):
    config.write_config({"future_stage": {"x": 1}, "detective": {"new_knob": "v"}})
    cfg = config.load_config()
    assert cfg["future_stage"] == {"x": 1}
    assert cfg["detective"]["new_knob"] == "v"


def test_write_config_atomic_and_owner_only(profile_env):
    path = config.write_config({"pii": {"default_max_files": 10}})
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"pii": {"default_max_files": 10}}
    assert not list(path.parent.glob(".config-*.tmp"))  # no droppings


def test_env_overrides_win(profile_env, monkeypatch):
    config.write_config({"jira": {"base_url": "https://file.example"}})
    monkeypatch.setenv("JIRA_BASE_URL", "https://env.example")
    monkeypatch.setenv("HERMES_CI_TRIAGE_ALLOW_PRIVATE", "1")
    cfg = config.load_config()
    assert cfg["jira"]["base_url"] == "https://env.example"
    assert cfg["triage"]["allow_private"] is True


def test_float_accepts_int(profile_env):
    config.write_config({"jira": {"sync_min_interval": 30}})
    cfg = config.load_config()
    assert cfg["jira"]["sync_min_interval"] == 30.0
    assert isinstance(cfg["jira"]["sync_min_interval"], float)
