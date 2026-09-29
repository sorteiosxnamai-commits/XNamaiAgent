from app.config import Settings
from app.workspace_configuration import _apply_overrides, load_workspace_settings


def test_only_known_setting_attributes_are_applied():
    settings = Settings(_env_file=None)
    result = _apply_overrides(settings, {
        "app_name": "MaiAgent",
        "openai_agent_name": "MaiAgent",
        "unknown_or_secret": "must-not-enter-settings",
    })
    assert result.app_name == "MaiAgent"
    assert result.openai_agent_name == "MaiAgent"
    assert not hasattr(result, "unknown_or_secret")


def test_database_failure_preserves_environment_settings(monkeypatch):
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://invalid.example/db",
        CHATBO_WORKSPACE_ID="aa774d20-509f-4d54-865b-7a5de22b6d30",
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("app.workspace_configuration.connect_database_url", fail)
    assert load_workspace_settings(settings) is settings
