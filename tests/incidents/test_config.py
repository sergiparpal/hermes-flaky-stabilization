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
