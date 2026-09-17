from app.config import Settings, get_settings


def test_openai_model_fallback_is_gpt_4_1_mini():
    assert Settings.model_fields["openai_model"].default == "gpt-4.1-mini"


def test_no_payment_gateway_settings_exist():
    """A config não pode mais ligar/desligar um gateway de pagamento legado.

    Substitui ``test_pix_direct_defaults_are_safe_off``: os campos que aquele
    teste mantinha "seguros em off" deixaram de existir junto com os módulos
    PIX/Mercado Pago. Sem campo não há flag para ligar por engano.
    """
    forbidden = {
        "pix_direct_enabled",
        "pix_exp_min",
        "mp_base_url",
        "mp_access_token",
        "mercadopago_access_token",
    }
    present = forbidden & set(Settings.model_fields)
    assert not present, f"config ainda expõe campos de pagamento: {sorted(present)}"


def test_history_window_defaults_separate_model_and_recovery():
    assert Settings.model_fields["agent_history_limit"].default == 12
    assert Settings.model_fields["agent_history_hard_cap"].default == 80
    assert Settings.model_fields["agent_max_recent_turns"].default == 8


def test_legacy_history_limit_200_does_not_crash_and_coerces_to_model_window(monkeypatch):
    """Production Vercel still had AGENT_HISTORY_LIMIT=200 from the old setup."""
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_HISTORY_LIMIT", "200")
    monkeypatch.setenv("AGENT_HISTORY_HARD_CAP", "80")
    try:
        settings = Settings()
    finally:
        get_settings.cache_clear()
    assert settings.agent_history_limit == 12
    assert settings.agent_history_hard_cap == 80


def test_llm_budget_defaults_are_conservative_etapa6():
    assert Settings.model_fields["agent_llm_budget_enabled"].default is True
    assert Settings.model_fields["agent_max_llm_calls_per_turn"].default == 2
    assert Settings.model_fields["agent_max_llm_calls_per_turn_complex"].default == 4
    assert Settings.model_fields["agent_critique_mode"].default == "shadow"
    assert Settings.model_fields["agent_critique_max_retries"].default == 1
    assert Settings.model_fields["agent_critique_llm_on_risk_only"].default is True
    assert Settings.model_fields["agent_critique_shadow_sample_rate"].default == 0.0
    assert Settings.model_fields["agent_quality_judge_mode"].default == "off"
    assert Settings.model_fields["agent_quality_judge_risk_threshold"].default == 70
    assert Settings.model_fields["agent_quality_judge_sample_rate"].default == 0.0
    assert Settings.model_fields["agent_presenter_mode"].default == "thin"


def test_rollout_defaults_etapa12():
    assert Settings.model_fields["agent_rollout_profile"].default == "full"
    assert Settings.model_fields["agent_emergency_rollback"].default is False
    assert Settings.model_fields["agent_rollout_alert_enabled"].default is True
    assert Settings.model_fields["agent_rollout_alert_window"].default == 40
    assert Settings.model_fields["agent_rollout_fallback_alert_rate"].default == 0.25


def test_legacy_critique_mode_on_coerces_to_enforce(monkeypatch):
    """Production Vercel had AGENT_CRITIQUE_MODE=on, which is not a Literal value."""
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_MODE", "on")
    try:
        settings = Settings()
    finally:
        get_settings.cache_clear()
    assert settings.agent_critique_mode == "enforce"


def test_persona_and_memory_rollout_defaults():
    assert Settings.model_fields["agent_db_persona_enabled"].default is True
    assert Settings.model_fields["agent_memory_proposals_enabled"].default is True
    assert Settings.model_fields["agent_memory_auto_apply_enabled"].default is False
    assert Settings.model_fields["agent_contact_memory_in_prompt_enabled"].default is True
    assert Settings.model_fields["agent_conversation_summary_enabled"].default is False
    assert (
        Settings.model_fields["agent_conversation_summary_in_prompt_enabled"].default
        is False
    )
    assert Settings.model_fields["agent_learning_auto_promote"].default is False
    assert Settings.model_fields["agent_learning_auto_activate"].default is False
    assert Settings.model_fields["agent_full_obs_logs"].default is False
    assert Settings.model_fields["agent_http_obs_logs"].default is False


# --- Gate 5 / Task 11: identidade técnica XNamai e remoção das envs Tray ---


def test_app_name_default_is_xnamai():
    assert Settings.model_fields["app_name"].default == "XNamaiAgent"


def test_openai_agent_name_default_is_xnamai():
    """Campo puramente técnico: nenhum leitor no runtime, não chega a canal/prompt/banco."""
    assert Settings.model_fields["openai_agent_name"].default == "XNamaiAgent"


def test_no_tray_env_fields():
    tray_fields = [name for name in Settings.model_fields if "tray" in name.lower()]
    assert tray_fields == []


def test_no_tray_env_aliases():
    aliases = [
        str(field.alias or "")
        for field in Settings.model_fields.values()
    ]
    assert [alias for alias in aliases if "TRAY" in alias.upper()] == []


def test_brevo_identity_defaults_are_xnamai():
    """Envio e supressão de eco usam a mesma identidade técnica neutra."""
    assert Settings.model_fields["brevo_agent_name"].default == "XNamaiAgent"
    assert Settings.model_fields["brevo_received_from"].default == "XNamaiAgent"


def test_persona_lookup_keys_are_untouched():
    assert Settings.model_fields["agent_persona_tenant_id"].default == "xnamai"
    assert Settings.model_fields["agent_persona_key"].default == "xnamai_commercial"


def test_settings_boot_without_env_file():
    settings = Settings(_env_file=None)
    assert settings.app_name == "XNamaiAgent"
