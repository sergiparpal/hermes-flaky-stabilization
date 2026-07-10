"""IncidentsConfig path resolution (audit finding 11)."""

import os

from jira_incidents import config as config_mod


def test_effective_db_path_expands_tilde():
    cfg = config_mod.IncidentsConfig.from_dict({"db_path": "~/jira/idx.db"})
    out = cfg.effective_db_path("/some/home")
    assert "~" not in out
    assert out.endswith(os.path.join("jira", "idx.db"))
    assert out.startswith(os.path.expanduser("~"))


def test_effective_db_path_expands_hermes_home_placeholder():
    cfg = config_mod.IncidentsConfig.from_dict({"db_path": "$HERMES_HOME/idx.db"})
    assert cfg.effective_db_path("/h") == os.path.join("/h", "idx.db")


def test_effective_db_path_defaults_under_home():
    cfg = config_mod.IncidentsConfig.from_dict({})
    out = cfg.effective_db_path("/h")
    assert out == os.path.join("/h", config_mod.DB_FILE)


# ---------------------------------------------------------------------------
# run_sync_kwargs: a configured root_cause_field must actually be requested
# from Jira, or extract_root_cause step 1 can never fire (silent-no-op finding).
# ---------------------------------------------------------------------------

def test_run_sync_kwargs_requests_configured_root_cause_field():
    from jira_incidents import jira_client as jc
    cfg = config_mod.IncidentsConfig.from_dict({"root_cause_field": "customfield_10050"})
    fields = cfg.run_sync_kwargs()["fields"]
    assert fields is not None and "customfield_10050" in fields
    # the defaults are preserved alongside the appended custom field
    for f in jc.DEFAULT_SEARCH_FIELDS:
        assert f in fields


def test_run_sync_kwargs_appends_root_cause_field_to_explicit_fields():
    cfg = config_mod.IncidentsConfig.from_dict(
        {"root_cause_field": "customfield_1", "fields": ["summary", "updated"]})
    assert cfg.run_sync_kwargs()["fields"] == ["summary", "updated", "customfield_1"]


def test_run_sync_kwargs_does_not_duplicate_root_cause_field():
    cfg = config_mod.IncidentsConfig.from_dict(
        {"root_cause_field": "summary", "fields": ["summary", "updated"]})
    assert cfg.run_sync_kwargs()["fields"] == ["summary", "updated"]


def test_run_sync_kwargs_fields_stay_none_without_root_cause_field():
    cfg = config_mod.IncidentsConfig.from_dict({})
    assert cfg.run_sync_kwargs()["fields"] is None
