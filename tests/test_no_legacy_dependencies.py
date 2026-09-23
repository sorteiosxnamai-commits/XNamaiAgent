"""Guards against reintroducing inactive commerce integrations in app, api and scripts."""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_DIRS = ("app", "api", "scripts")
FORBIDDEN_MODULES = {"tray_tools", "tray_adapter_client"}


def _runtime_python_files():
    for base in RUNTIME_DIRS:
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Todo nome de módulo que ``path`` importa, em qualquer forma de import.

    Cobre ``import a.b``, ``import a.b as c``, ``from a.b import c`` e também
    ``from a import b`` — esta última é a forma que a versão anterior deixava
    passar, porque o módulo importado aparece só no *alias*, nunca em
    ``node.module``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.update(node.module.split("."))
            for alias in node.names:
                # ``from app import tray_tools`` / ``from . import tray_tools``
                found.add(alias.name)
    return found


RUNTIME_FILES = sorted(_runtime_python_files(), key=str)


def test_runtime_scan_is_not_empty():
    """Sem isto a guarda inteira pode passar a vácuo fora da raiz do repo."""
    assert len(RUNTIME_FILES) > 50, (
        f"varredura de runtime coletou apenas {len(RUNTIME_FILES)} arquivos "
        f"a partir de {REPO_ROOT}"
    )
    names = {path.name for path in RUNTIME_FILES}
    assert {"config.py", "sales_agent.py", "index.py"} <= names


def test_import_scanner_catches_every_import_form(tmp_path):
    """A própria guarda é testada: um alias de ImportFrom não pode escapar."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "\n".join(
            (
                "import app.tray_tools",
                "from app import tray_adapter_client",
                "from app.mercadopago_client import Foo",
                "import app.pix_settlement as settle",
                "from . import tray_tools",
            )
        ),
        encoding="utf-8",
    )
    found = _imported_modules(sample)
    assert {"tray_tools", "tray_adapter_client", "mercadopago_client", "pix_settlement"} <= found


@pytest.mark.parametrize("path", RUNTIME_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_runtime_does_not_import_legacy_commerce_modules(path):
    offending = _imported_modules(path) & FORBIDDEN_MODULES
    assert not offending, f"{path} ainda importa {sorted(offending)}"


def test_legacy_commerce_modules_are_deleted():
    for name in ("app/tray_tools.py", "app/tray_adapter_client.py"):
        assert not (REPO_ROOT / name).exists(), f"{name} ainda existe"


# --- Gate 3 / Task 8: banco de sorteio fora do runtime ---

SORTEIO_DB_TOKENS = (
    "SORTEIO_DATABASE_URL",
    "get_sorteio_conn",
    "resolved_sorteio_database_url",
)


def test_runtime_has_no_sorteio_database_dependency():
    offenders = []
    for path in _runtime_python_files():
        text = path.read_text(encoding="utf-8")
        hits = [token for token in SORTEIO_DB_TOKENS if token in text]
        if hits:
            offenders.append(f"{path}: {hits}")
    assert not offenders, f"dependência de banco de sorteio em: {offenders}"


def test_runtime_configuration_declares_no_legacy_commerce_envs():
    """Exemplos e scripts operacionais nao podem manter envs do stack removido."""
    forbidden = (
        "SORTEIO_DATABASE_URL",
        "TRAY_ADAPTER_URL",
        "TRAY_ADAPTER_TOKEN",
        "MERCADOPAGO_ACCESS_TOKEN",
        "MP_ACCESS_TOKEN",
        "PIX_DIRECT_ENABLED",
        "unrelated.example",
        "unrelated.example",
    )
    files = (
        REPO_ROOT / ".env.example",
        REPO_ROOT / "app" / "config.py",
        REPO_ROOT / "scripts" / "package_release.py",
    )
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        hits = [token for token in forbidden if token in text]
        if hits:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {hits}")
    assert not offenders, f"envs legadas ainda declaradas: {offenders}"


def test_normalize_phone_still_available():
    from app.repository import normalize_phone

    assert normalize_phone("+55 (11) 99999-9999")


def test_format_cents_to_brl_still_available():
    from app.repository import format_cents_to_brl

    assert format_cents_to_brl(1990)


def test_settings_boot_without_sorteio_database_url(monkeypatch):
    """O boot não pode depender de SORTEIO_DATABASE_URL no ambiente."""
    from app.config import Settings

    monkeypatch.delenv("SORTEIO_DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings is not None
    assert "sorteio_database_url" not in Settings.model_fields


# --- Gate 4 / Task 9: PIX/Mercado Pago fora do runtime ---

PIX_MP_MODULES = (
    "app/mercadopago_client.py",
    "app/pix_payment_service.py",
    "app/pix_payment_repository.py",
    "app/pix_checkout_service.py",
    "app/pix_settlement.py",
    "app/pix_webhook_api.py",
)

FORBIDDEN_PIX_MODULES = {
    "mercadopago_client",
    "pix_payment_service",
    "pix_payment_repository",
    "pix_checkout_service",
    "pix_settlement",
    "pix_webhook_api",
}


def test_no_active_pix_routes():
    import api.index as index

    paths = {getattr(r, "path", "") for r in index.app.routes}
    offending = [p for p in paths if "payments" in p or "pix" in p]
    assert not offending, f"rotas de pagamento ainda ativas: {sorted(offending)}"


@pytest.mark.parametrize("path", RUNTIME_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_runtime_does_not_import_pix_mercadopago_modules(path):
    offending = _imported_modules(path) & FORBIDDEN_PIX_MODULES
    assert not offending, f"{path} ainda importa {sorted(offending)}"


def test_pix_mercadopago_modules_are_deleted():
    for name in PIX_MP_MODULES:
        assert not (REPO_ROOT / name).exists(), f"{name} ainda existe"


def test_settings_have_no_mercadopago_fields():
    from app.config import Settings

    forbidden = {
        "pix_direct_enabled",
        "mp_access_token",
        "mercadopago_access_token",
        "mp_base_url",
        "pix_exp_min",
    }
    present = forbidden & set(Settings.model_fields)
    assert not present, f"config ainda expõe campos de pagamento: {sorted(present)}"
    assert not hasattr(Settings, "resolved_mp_access_token")
    assert not hasattr(Settings, "pix_notification_url")


# --- Gate 6 / Task 12: nenhum domínio de marca legada é confiável por padrão ---

def test_no_legacy_brand_domain_is_trusted_by_default():
    """A validação factual não pode aceitar link de marca legada como fato oficial.

    Sem fonte oficial configurada, o allowlist é vazio: fail-closed, mesma regra
    do ``NullCommerceProvider``.
    """
    from app.config import Settings

    default = Settings.model_fields["agent_trusted_fact_domains"].default
    assert default == ""
    for token in ("sorteioxnamai", "xnamaisorteios", "xnamai"):
        assert token not in str(default).casefold()


def test_runtime_never_hardcodes_a_legacy_brand_url():
    """Nenhum módulo de runtime pode conter URL da marca legada.

    Cobre o resíduo encontrado no Gate 6 em ``app/simulation.py``, onde a
    resposta de simulação ainda encaminhava o cliente ao site antigo.
    """
    forbidden = ("unrelated.example", "unrelated.example", "unrelated.example")
    offenders = []
    for path in RUNTIME_FILES:
        text = path.read_text(encoding="utf-8").casefold()
        hits = [token for token in forbidden if token in text]
        if hits:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {hits}")
    assert not offenders, f"URL de marca legada no runtime: {offenders}"


def test_xnamai_runtime_has_no_inactive_provider_dependencies():
    """Guarda-sintese pedida para impedir a reintroducao do stack legado."""
    forbidden_imports = FORBIDDEN_MODULES | FORBIDDEN_PIX_MODULES
    import_offenders = []
    token_offenders = []
    for path in RUNTIME_FILES:
        imported = _imported_modules(path) & forbidden_imports
        if imported:
            import_offenders.append(
                f"{path.relative_to(REPO_ROOT)}: {sorted(imported)}"
            )
        text = path.read_text(encoding="utf-8")
        hits = [token for token in SORTEIO_DB_TOKENS if token in text]
        if hits:
            token_offenders.append(f"{path.relative_to(REPO_ROOT)}: {hits}")

    deleted_modules = (
        "app/tray_tools.py",
        "app/tray_adapter_client.py",
        *PIX_MP_MODULES,
    )
    existing_modules = [name for name in deleted_modules if (REPO_ROOT / name).exists()]

    assert not import_offenders, f"imports legados no runtime: {import_offenders}"
    assert not token_offenders, f"dependencias do DB de sorteio: {token_offenders}"
    assert not existing_modules, f"modulos legados ainda existem: {existing_modules}"


def test_runtime_bootstrap_creates_no_payment_gateway_table():
    """``ensure_tables`` não pode mais provisionar o schema do gateway removido.

    O DDL de ``ai_pix_payments`` rodava a cada ``ensure_tables()`` — que é
    chamado em dezenas de caminhos de runtime — provisionando armazenamento de
    uma feature que não existe mais. Nenhum ``DROP`` foi introduzido: bases
    existentes ficam intactas, apenas não se cria mais a tabela morta.
    """
    source = (REPO_ROOT / "app" / "db.py").read_text(encoding="utf-8").casefold()
    for token in ("ai_pix_payments", "mp_payment_id", "mercadopago"):
        assert token not in source, f"DDL legado ainda presente em app/db.py: {token}"
    assert "drop table" not in source, "a limpeza não pode introduzir DROP"
