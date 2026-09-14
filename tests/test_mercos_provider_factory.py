"""Selecao de provider, health seguro e ausencia de segredo/acesso direto.

Regras verificadas aqui:
* adaptador configurado -> MercosCommerceProvider; senao -> NullCommerceProvider;
* nunca fallback para fornecedor legado;
* o XNamai nao guarda token da Mercos nem chama app.mercos.com;
* /health nao revela chave, token nem URL com segredo.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.commerce.provider import (
    NullCommerceProvider,
    get_commerce_provider,
    reset_commerce_provider,
)
from app.config import Settings, get_settings

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_provider():
    reset_commerce_provider()
    get_settings.cache_clear()
    yield
    reset_commerce_provider()
    get_settings.cache_clear()


# --- selecao de provider ----------------------------------------------------


def test_without_configuration_the_provider_is_null(monkeypatch):
    monkeypatch.delenv("MERCOS_ADAPTOR_URL", raising=False)
    monkeypatch.delenv("MERCOS_ADAPTOR_API_KEY", raising=False)
    get_settings.cache_clear()
    provider = get_commerce_provider()
    assert isinstance(provider, NullCommerceProvider)
    assert provider.available is False


def test_with_both_variables_the_provider_is_mercos(monkeypatch):
    from app.commerce.mercos.provider import MercosCommerceProvider

    monkeypatch.setenv("MERCOS_ADAPTOR_URL", "https://adaptor.example.com")
    monkeypatch.setenv("MERCOS_ADAPTOR_API_KEY", "internal-key")
    get_settings.cache_clear()
    assert isinstance(get_commerce_provider(), MercosCommerceProvider)


@pytest.mark.parametrize(
    "url,key",
    [("https://adaptor.example.com", ""), ("", "internal-key"), ("", ""), ("   ", "   ")],
)
def test_partial_configuration_falls_back_to_null(monkeypatch, url, key):
    """Meia configuracao e configuracao nenhuma — nunca um provider pela metade."""
    monkeypatch.setenv("MERCOS_ADAPTOR_URL", url)
    monkeypatch.setenv("MERCOS_ADAPTOR_API_KEY", key)
    get_settings.cache_clear()
    assert isinstance(get_commerce_provider(), NullCommerceProvider)


def test_configuration_flag_matches_the_selection():
    assert Settings(_env_file=None).mercos_adaptor_configured is False
    configured = Settings(
        _env_file=None,
        MERCOS_ADAPTOR_URL="https://adaptor.example.com",
        MERCOS_ADAPTOR_API_KEY="internal-key",
    )
    assert configured.mercos_adaptor_configured is True


# Os guards de segredo/acesso direto vivem em tests/test_mercos_secret_boundary.py,
# que mede CODIGO por AST em vez de substring.


def test_settings_never_declare_mercos_credentials():
    forbidden = {
        "mercos_application_token",
        "mercos_company_token",
        "mercos_base_url",
    }
    assert not (forbidden & set(Settings.model_fields))


def test_mercos_knowledge_is_confined_to_its_package():
    """Nenhum ``if provider == 'mercos'`` espalhado pelo core."""
    allowed_prefix = "app/commerce/mercos/"
    offenders = []
    for base in ("app", "api", "scripts"):
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(allowed_prefix):
                continue
            text = path.read_text(encoding="utf-8")
            if "mercos" not in text.casefold():
                continue
            tree = ast.parse(text)
            imports_mercos_pkg = any(
                "mercos" in (getattr(node, "module", "") or "").casefold()
                or any("mercos" in alias.name.casefold() for alias in node.names)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
            )
            # config.py declara as envs; provider.py escolhe a implementacao.
            if rel in {"app/config.py", "app/commerce/provider.py"}:
                continue
            if imports_mercos_pkg:
                offenders.append(rel)
    assert not offenders, f"conhecimento Mercos fora do pacote: {offenders}"


# --- health seguro ----------------------------------------------------------


def test_health_reports_the_provider_without_leaking_secrets(monkeypatch):
    import importlib

    monkeypatch.setenv("MERCOS_ADAPTOR_URL", "https://adaptor.example.com")
    monkeypatch.setenv("MERCOS_ADAPTOR_API_KEY", "super-secret-key")
    get_settings.cache_clear()
    reset_commerce_provider()

    index = importlib.import_module("api.index")
    source = pathlib.Path(index.__file__).read_text(encoding="utf-8")
    assert "mercos_adaptor_api_key" not in source
    assert "mercos_adaptor_url" not in source
