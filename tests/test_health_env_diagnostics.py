"""Instrumentação TEMPORÁRIA de diagnóstico.

Separa "a Vercel não entregou a variável" de "o Settings perdeu a variável",
comparando os.environ (raw) contra get_settings() para DATABASE_URL e
OPENAI_API_KEY. Só booleanos e comprimentos — nunca valores.
"""

from types import SimpleNamespace

import pytest

DIAGNOSTIC_KEYS = {
    "raw_database_url_present",
    "raw_database_url_len",
    "settings_database_url_present",
    "settings_database_url_len",
    "raw_openai_api_key_present",
    "raw_openai_api_key_len",
    "settings_openai_api_key_present",
    "settings_openai_api_key_len",
}

SECRET_DB = "postgresql://diag_user:diag_pass@db.example.test:5432/diagdb"
SECRET_KEY = "sk-proj-DIAGNOSTIC-NOT-A-REAL-KEY-0123456789"


def _settings(**overrides):
    values = {
        "openai_api_key": "",
        "openai_model": "gpt-test",
        "tray_adapter_url": "",
        "tray_adapter_token": "",
        "app_name": "test",
        "dry_run": True,
        "environment": "test",
        "database_url": "",
        "brevo_api_key": "",
        "brevo_agent_id": "",
        "brevo_agent_email": "",
        "brevo_agent_name": "",
        "brevo_sender_number": "",
        "brevo_reply_mode": "dry_run",
        "brevo_webhook_secret": "",
        "audio_inbound_enabled": True,
        "audio_outbound_enabled": True,
        "supabase_url": "",
        "supabase_service_key": "",
        "max_reply_chars": 900,
        "admin_api_token": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _health(monkeypatch, settings):
    import api.index as index

    monkeypatch.setattr(index, "get_settings", lambda: settings)
    return await index.health()


@pytest.mark.asyncio
async def test_health_exposes_env_diagnostics_block(monkeypatch):
    payload = await _health(monkeypatch, _settings())
    assert DIAGNOSTIC_KEYS.issubset(set(payload["env_diagnostics"]))


@pytest.mark.asyncio
async def test_case_a_raw_absent_and_settings_absent(monkeypatch):
    """Caso A: a Vercel não entregou nada ao processo."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    diag = (await _health(monkeypatch, _settings()))["env_diagnostics"]
    assert diag["raw_database_url_present"] is False
    assert diag["settings_database_url_present"] is False
    assert diag["raw_openai_api_key_present"] is False
    assert diag["settings_openai_api_key_present"] is False


@pytest.mark.asyncio
async def test_case_b_raw_present_but_settings_lost_it(monkeypatch):
    """Caso B: o processo recebeu, mas o Settings não carregou."""
    monkeypatch.setenv("DATABASE_URL", SECRET_DB)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_KEY)
    diag = (await _health(monkeypatch, _settings()))["env_diagnostics"]
    assert diag["raw_database_url_present"] is True
    assert diag["raw_database_url_len"] == len(SECRET_DB)
    assert diag["settings_database_url_present"] is False
    assert diag["settings_database_url_len"] == 0
    assert diag["raw_openai_api_key_present"] is True
    assert diag["raw_openai_api_key_len"] == len(SECRET_KEY)
    assert diag["settings_openai_api_key_present"] is False


@pytest.mark.asyncio
async def test_case_c_raw_present_and_settings_present(monkeypatch):
    """Caso C: tudo chegou — o dado analisado era de outro deployment."""
    monkeypatch.setenv("DATABASE_URL", SECRET_DB)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_KEY)
    settings = _settings(database_url=SECRET_DB, openai_api_key=SECRET_KEY)
    diag = (await _health(monkeypatch, settings))["env_diagnostics"]
    assert diag["settings_database_url_present"] is True
    assert diag["settings_database_url_len"] == len(SECRET_DB)
    assert diag["settings_openai_api_key_present"] is True
    assert diag["settings_openai_api_key_len"] == len(SECRET_KEY)


@pytest.mark.asyncio
async def test_diagnostics_never_leak_values(monkeypatch):
    """O bloco pode expor presença e comprimento, jamais conteúdo."""
    monkeypatch.setenv("DATABASE_URL", SECRET_DB)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_KEY)
    settings = _settings(database_url=SECRET_DB, openai_api_key=SECRET_KEY)
    payload = await _health(monkeypatch, settings)
    rendered = str(payload)

    assert SECRET_DB not in rendered
    assert SECRET_KEY not in rendered
    # nem prefixo, nem sufixo, nem fragmento
    assert "sk-proj-" not in rendered
    assert "diag_pass" not in rendered
    assert SECRET_KEY[:8] not in rendered
    assert SECRET_KEY[-8:] not in rendered
    assert all(isinstance(v, (bool, int)) for v in payload["env_diagnostics"].values())
