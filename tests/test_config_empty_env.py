"""Vercel entrega env vars tipadas como "" e o boot quebrava com 120 erros.

Todos os testes usam _env_file=None: o comportamento provado aqui é o do
ambiente, não o do .env local.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(monkeypatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_empty_bool_env_falls_back_to_default(monkeypatch):
    assert _settings(monkeypatch, DRY_RUN="").dry_run is True


def test_empty_int_env_falls_back_to_default(monkeypatch):
    assert _settings(monkeypatch, AGENT_MAX_RECENT_TURNS="").agent_max_recent_turns == 8


def test_empty_float_env_falls_back_to_default(monkeypatch):
    settings = _settings(monkeypatch, OPENAI_TIMEOUT_SECONDS="")
    assert settings.openai_timeout_seconds == 45.0


def test_empty_literal_env_falls_back_to_default(monkeypatch):
    assert _settings(monkeypatch, OPENAI_API_MODE="").openai_api_mode == "responses"


def test_all_empty_typed_envs_together_do_not_break_boot(monkeypatch):
    """A assinatura exata do runtime log: vários tipos vazios na mesma request."""
    settings = _settings(
        monkeypatch,
        DRY_RUN="",
        OPENAI_API_MODE="",
        OPENAI_TIMEOUT_SECONDS="",
        AGENT_MAX_RECENT_TURNS="",
        AGENT_ROLLOUT_PROFILE="",
        INSTAGRAM_STORY_CANARY_PERCENT="",
    )
    assert settings.dry_run is True
    assert settings.openai_api_mode == "responses"
    assert settings.openai_timeout_seconds == 45.0
    assert settings.agent_max_recent_turns == 8
    assert settings.agent_rollout_profile == "full"
    assert settings.instagram_story_canary_percent == 5.0


def test_non_empty_bool_still_overrides_default(monkeypatch):
    assert _settings(monkeypatch, DRY_RUN="false").dry_run is False


def test_non_empty_literal_still_overrides_default(monkeypatch):
    assert _settings(monkeypatch, OPENAI_API_MODE="shadow").openai_api_mode == "shadow"


def test_non_empty_numeric_still_overrides_default(monkeypatch):
    settings = _settings(
        monkeypatch, AGENT_MAX_RECENT_TURNS="3", OPENAI_TIMEOUT_SECONDS="12.5"
    )
    assert settings.agent_max_recent_turns == 3
    assert settings.openai_timeout_seconds == 12.5


def test_real_secret_strings_are_still_read(monkeypatch):
    settings = _settings(
        monkeypatch,
        OPENAI_API_KEY="sk-not-a-real-key",
        ADMIN_API_TOKEN="Bearer admin-token-value",
        YCLOUD_WHATSAPP_FROM="+5511999999999",
    )
    assert settings.openai_api_key == "sk-not-a-real-key"
    assert settings.admin_api_token == "admin-token-value"
    assert settings.ycloud_whatsapp_from == "+5511999999999"


def test_empty_secret_strings_fall_back_to_their_own_defaults(monkeypatch):
    """Strings vazias nunca quebraram o boot; seguem resolvendo para o default."""
    settings = _settings(monkeypatch, ADMIN_API_TOKEN="", BREVO_WEBHOOK_SECRET="")
    assert settings.admin_api_token == ""
    assert settings.brevo_webhook_secret == ""


@pytest.mark.parametrize(
    "env_name,field_name",
    [
        ("OPENAI_REASONING_EFFORT", "openai_reasoning_effort"),
        ("OPENAI_TEXT_VERBOSITY", "openai_text_verbosity"),
    ],
)
class TestOptionalLiteralsKeepEmptyMeaning:
    """"" é valor legítimo nestes dois campos: openai_gateway usa a string
    vazia para NÃO enviar reasoning.effort / text.verbosity à OpenAI.
    env_ignore_empty não pode descartá-los, ao contrário dos outros campos.
    """

    def test_absent_env_uses_default_medium(self, monkeypatch, env_name, field_name):
        monkeypatch.delenv(env_name, raising=False)
        assert getattr(Settings(_env_file=None), field_name) == "medium"

    def test_empty_env_stays_empty(self, monkeypatch, env_name, field_name):
        monkeypatch.setenv(env_name, "")
        assert getattr(Settings(_env_file=None), field_name) == ""

    def test_explicit_value_is_used(self, monkeypatch, env_name, field_name):
        monkeypatch.setenv(env_name, "low")
        assert getattr(Settings(_env_file=None), field_name) == "low"

    def test_init_kwarg_still_wins_over_empty_env(
        self, monkeypatch, env_name, field_name
    ):
        monkeypatch.setenv(env_name, "")
        settings = Settings(_env_file=None, **{env_name: "high"})
        assert getattr(settings, field_name) == "high"


def test_invalid_non_empty_value_still_raises(monkeypatch):
    """env_ignore_empty ignora "" — não deve mascarar valor inválido de verdade."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, AGENT_MAX_RECENT_TURNS="not-a-number")
