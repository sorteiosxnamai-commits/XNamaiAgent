"""Persona e comercio sao dominios independentes.

O resíduo que estes testes impedem: o catalogo Mercos nascer particionado pelo
tenant da PERSONA (`AGENT_PERSONA_TENANT_ID=newstore`). Isso reintroduziria a
marca legada por uma porta lateral — sem nenhum import de Tray, sem nenhuma
chamada NewStore, apenas pela chave de particionamento dos dados novos.

As duas chaves seguem existindo e servem a coisas diferentes:

    AGENT_PERSONA_TENANT_ID = newstore   -> lookup da persona (PROTEGIDO, intocado)
    COMMERCE_TENANT_ID      = xnamai     -> sync state + indice de catalogo
"""

from __future__ import annotations

import pathlib

import pytest

from app.config import Settings

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MERCOS_DIR = REPO_ROOT / "app" / "commerce" / "mercos"


def _mercos_files():
    return [p for p in sorted(MERCOS_DIR.rglob("*.py")) if "__pycache__" not in p.parts]


# --- guard arquitetural -----------------------------------------------------


def test_scan_is_not_empty():
    """Sem isto a guarda inteira pode passar a vacuo."""
    files = _mercos_files()
    assert len(files) >= 10
    assert any(p.name == "sync_state.py" for p in files)


@pytest.mark.parametrize("path", _mercos_files(), ids=lambda p: p.name)
def test_mercos_package_never_mentions_the_legacy_tenant(path):
    text = path.read_text(encoding="utf-8").casefold()
    assert "newstore" not in text, f"{path.name} cita o tenant da marca legada"


@pytest.mark.parametrize("path", _mercos_files(), ids=lambda p: p.name)
def test_mercos_package_never_reads_the_persona_tenant(path):
    """Comercio nao pode se particionar pela chave de lookup da persona."""
    text = path.read_text(encoding="utf-8")
    assert "agent_persona_tenant_id" not in text, f"{path.name} usa o tenant da persona"


def test_migration_023_has_no_legacy_tenant_and_no_default():
    sql = (REPO_ROOT / "sql" / "023_mercos_sync_state.sql").read_text(encoding="utf-8")
    assert "newstore" not in sql.casefold()
    # Sem DEFAULT: insercao sem tenant deve FALHAR, nunca cair em outra marca.
    tenant_line = next(
        line for line in sql.splitlines() if line.strip().startswith("tenant_id")
    )
    assert "DEFAULT" not in tenant_line.upper(), tenant_line


@pytest.mark.parametrize("path", _mercos_files(), ids=lambda p: p.name)
def test_mercos_package_hardcodes_no_commerce_tenant(path):
    """Nenhum literal de tenant no pacote — nem o legado, nem o novo.

    ``tenant_id="xnamai"`` seria uma SEGUNDA fonte de verdade. No dia em que ela
    divergisse do Settings, o catalogo seria gravado numa particao e lido de
    outra: busca vazia, sem erro nenhum, e o agente respondendo "nao temos".
    """
    import re

    text = path.read_text(encoding="utf-8")
    hits = re.findall(r"""tenant_id\s*[:=]\s*['"]([^'"]*)['"]""", text)
    assert not hits, f"{path.name} fixa tenant literal: {hits}"


def test_only_settings_defines_the_commerce_tenant_default():
    """A unica fonte de verdade e ``Settings.commerce_tenant_id``."""
    config = (REPO_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert 'alias="COMMERCE_TENANT_ID"' in config
    assert 'default="xnamai"' in config

    for path in _mercos_files():
        assert "COMMERCE_TENANT_ID" not in path.read_text(encoding="utf-8"), (
            f"{path.name} le a env diretamente em vez de usar Settings"
        )


def test_provider_refuses_to_invent_a_tenant():
    from app.commerce.mercos.client import MercosAdaptorClient
    from app.commerce.mercos.provider import MercosCommerceProvider

    client = MercosAdaptorClient(base_url="https://a.example.com", api_key="k")
    for empty in ("", "   ", None):
        with pytest.raises(ValueError):
            MercosCommerceProvider(client, index=object(), tenant_id=empty)
    with pytest.raises(TypeError):
        MercosCommerceProvider(client, index=object())


def test_factory_always_forwards_the_settings_tenant():
    """Trocar COMMERCE_TENANT_ID move provider, sync state e writer juntos."""
    from app.commerce.mercos.provider import build_mercos_provider

    for tenant in ("xnamai", "outro_comercio", "terceiro"):
        provider = build_mercos_provider(
            _settings(COMMERCE_TENANT_ID=tenant), index=object()
        )
        assert provider._tenant_id == tenant
        assert provider._sync_state._tenant_id == tenant


def test_sync_state_store_refuses_to_invent_a_tenant():
    """Default oculto foi como o catalogo anterior ficou preso a uma marca."""
    from app.commerce.mercos.sync_state import DatabaseSyncStateStore

    for empty in ("", "   "):
        with pytest.raises(ValueError):
            DatabaseSyncStateStore(tenant_id=empty)
    with pytest.raises(TypeError):
        DatabaseSyncStateStore()  # tenant_id e keyword obrigatorio


# --- os dois dominios coexistem sem se misturar -----------------------------


def _settings(**overrides):
    fields = {
        "AGENT_PERSONA_TENANT_ID": "newstore",
        "COMMERCE_TENANT_ID": "xnamai",
        "MERCOS_ADAPTOR_URL": "https://adaptor.example.com",
        "MERCOS_ADAPTOR_API_KEY": "internal-key",
        "DATABASE_URL": "postgresql://x/y",
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)


def test_persona_tenant_and_commerce_tenant_are_independent():
    settings = _settings()
    assert settings.agent_persona_tenant_id == "newstore"
    assert settings.commerce_tenant_id == "xnamai"


def test_commerce_tenant_defaults_to_xnamai_not_to_the_persona():
    assert Settings(_env_file=None).commerce_tenant_id == "xnamai"


def test_provider_partitions_by_the_commerce_tenant():
    """Sync state e indice recebem o tenant COMERCIAL, nunca o da persona."""
    from app.commerce.mercos.provider import build_mercos_provider

    provider = build_mercos_provider(_settings(), index=object())
    assert provider._tenant_id == "xnamai"
    assert provider._sync_state._tenant_id == "xnamai"


def test_changing_the_persona_tenant_does_not_move_the_catalog():
    """Renomear o tenant da persona nao pode reparticionar o catalogo."""
    from app.commerce.mercos.provider import build_mercos_provider

    provider = build_mercos_provider(
        _settings(AGENT_PERSONA_TENANT_ID="outra_marca"), index=object()
    )
    assert provider._tenant_id == "xnamai"


def test_changing_the_commerce_tenant_does_not_move_the_persona():
    settings = _settings(COMMERCE_TENANT_ID="outro_comercio")
    assert settings.commerce_tenant_id == "outro_comercio"
    assert settings.agent_persona_tenant_id == "newstore"


@pytest.mark.asyncio
async def test_sync_runner_uses_the_commerce_tenant():
    from app.commerce.mercos.sync_runner import run_product_sync

    seen = {}

    class _Store:
        def __init__(self, tenant_id):
            seen["store_tenant"] = tenant_id

        def read(self, provider, resource):
            from app.commerce.mercos.sync_state import SyncState

            return SyncState(provider, resource, missing_table=True)

    import app.commerce.mercos.sync_state as sync_state_module

    original = sync_state_module.DatabaseSyncStateStore
    sync_state_module.DatabaseSyncStateStore = lambda *, tenant_id: _Store(tenant_id)
    try:
        await run_product_sync(settings=_settings())
    finally:
        sync_state_module.DatabaseSyncStateStore = original

    assert seen["store_tenant"] == "xnamai"


def test_catalog_writer_and_reader_share_the_commerce_tenant():
    """Escrita e leitura precisam cair na MESMA particao, ou a busca volta vazia."""
    from app.commerce.mercos.catalog import ProductPageWriter
    from app.commerce.mercos.provider import build_mercos_provider

    provider = build_mercos_provider(_settings(), index=object())
    writer = ProductPageWriter(object(), tenant_id=provider._tenant_id)
    assert writer._tenant_id == provider._tenant_id == "xnamai"


# --- a persona continua intocada --------------------------------------------


def test_persona_lookup_keys_are_preserved():
    """PERSONA_PROTECTED_RESIDUE: estes valores NAO mudam nesta correcao."""
    settings = Settings(_env_file=None)
    assert settings.agent_persona_tenant_id == "newstore"
    assert settings.agent_persona_key == "newstore_commercial"
