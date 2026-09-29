"""Gate de legado: o XNamaiAgent nao pode voltar a carregar o produto anterior.

A varredura percorre o SISTEMA DE ARQUIVOS a partir da raiz do repositorio —
nunca ``git diff``/``git ls-files``. Em ZIP de release, CI isolado ou copia sem
``.git`` uma guarda baseada em git passaria a vacuo sem analisar nada.

Cobre codigo executavel, prompts, fallbacks, configuracao, SQL, documentacao e
o TEXTO RENDERIZADO que chega ao modelo (``tests/prompt_surface.py``). Imports
de modulos removidos sao guardados em ``test_no_legacy_dependencies.py``.

Fora do escopo, por construcao:
* ``tests/``: testes precisam NOMEAR os termos proibidos para afirmar que eles
  nao existem; o que dos testes chega ao modelo e coberto pelo render abaixo;
* diretorios locais nao versionados de ferramentas/sessao e caches;
* ``.env``: segredos locais, gitignored, nunca empacotados.

Toda excecao vive em ``ALLOWLIST`` com justificativa. Uma excecao que deixa de
ser necessaria FALHA o teste — a lista so pode encolher.
"""

from __future__ import annotations

import pathlib
import re
import unicodedata

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Termos do produto anterior. O texto e comparado apos remover acentos e
#: normalizar caixa, entao "Cartão Presente" e "cartao_presente" casam igual.
FORBIDDEN: dict[str, re.Pattern[str]] = {
    "newstore": re.compile(r"new[\s_-]*store"),
    "nsagent": re.compile(r"nsagent"),
    "sorteio": re.compile(r"sorteio"),
    "rifa": re.compile(r"\brifas?\b"),
    "raffle": re.compile(r"raffle"),
    "lotomania": re.compile(r"lotomania"),
    "cartao_presente": re.compile(r"cartao[\s_-]*presente"),
    "gift_card": re.compile(r"gift[\s_-]*card"),
    # "tray" sem letra antes: pega tray_live/TrayAdapter, nao "portray"/"astray".
    "tray": re.compile(r"(?<![a-z])tray"),
}

EXCLUDED_DIR_NAMES = frozenset({
    ".git",
    "tests",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "htmlcov",
    ".vercel",
    # Notas locais de ferramentas de agente/sessao: nao versionadas, nao sao fonte.
    ".superpowers",
    ".remember",
})
EXCLUDED_FILE_NAMES = frozenset({".env"})

TEXT_SUFFIXES = frozenset({
    ".py", ".sql", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".txt", ".sh", ".example", ".html", ".js", ".css",
})
TEXT_NAMES = frozenset({"Dockerfile", "Procfile", "Makefile", ".gitignore"})

_HISTORICAL_MIGRATION = (
    "migration historica possivelmente aplicada em producao; nunca reescrita. "
    "Correcao, se necessaria, vem em migration NOVA"
)
_HISTORICAL_DOC = (
    "registro historico de migracao/arquitetura anterior a neutralizacao; "
    "descreve o estado da epoca e nao e lido pelo runtime"
)

#: caminho relativo -> {termo: justificativa}
ALLOWLIST: dict[str, dict[str, str]] = {
    # --- compatibilidade historica: valores LIDOS, nunca mais produzidos ---
    "app/catalog_index.py": {
        "tray": "FactualSource aceita tray_live/tray_search gravados em ai_catalog_index; escrita nova usa commerce_*",
    },
    "app/fact_sources.py": {
        "tray": "tray_live/tray_search persistidos continuam com a autoridade correta; escrita nova usa commerce_*",
    },
    "app/story_commercial_policy.py": {
        "tray": "product_evidence ja persistido pode ter source=tray_api; escrita nova usa commerce_api",
    },
    "app/models.py": {
        "raffle": "dominio legado lido de cache/estado e convertido em out_of_scope; nao e oferecido ao modelo",
    },
    "app/commerce_context.py": {
        "raffle": "active_domain legado em commerce_state persistido; lido como None sem descartar o estado",
    },
    # --- referencia ainda necessaria ---
    "app/commerce/mercos/client.py": {
        "sorteio": "nome real da organizacao GitHub que hospeda o MercosAdaptor (sorteiosxnamai-commits)",
    },
    "docs/mercos_contract.md": {
        "sorteio": "nome real da organizacao GitHub que hospeda o MercosAdaptor (sorteiosxnamai-commits)",
    },
    "docs/club_contract.md": {
        "sorteio": "nome real da organizacao GitHub que hospeda o Club backend (sorteiosxnamai-commits)",
    },
    "app/privacy_scope.py": {
        "sorteio": "vocabulario de DETECCAO da guarda de privacidade de terceiros; remover so enfraquece a recusa",
        "cartao_presente": "vocabulario de DETECCAO da guarda de privacidade de terceiros; remover so enfraquece a recusa",
        "raffle": "nome da lista de deteccao herdada (RAFFLE_HISTORY_KEYWORDS), documentada para a paridade",
    },
    "app/prompt_compiler.py": {
        term: "vocabulario do DETECTOR que rejeita persona publicada do produto anterior (_has_legacy_store_identity)"
        for term in ("newstore", "nsagent", "sorteio", "lotomania")
    },
    "scripts/generate_eval_fixtures.py": {
        "sorteio": "entradas de eval que exigem out_of_scope para perguntas sobre o dominio anterior",
    },
    # --- migrations historicas ---
    "sql/002_ai_user_preferences.sql": {"sorteio": _HISTORICAL_MIGRATION},
    "sql/011_ai_pix_payments.sql": {"tray": _HISTORICAL_MIGRATION},
    "sql/012_ai_pix_payments_settlement_processing.sql": {"tray": _HISTORICAL_MIGRATION},
    "sql/013_drop_ai_user_preferences_users_fk.sql": {"sorteio": _HISTORICAL_MIGRATION},
    "sql/016_ai_catalog_cache.sql": {"tray": _HISTORICAL_MIGRATION},
    "sql/017_ai_catalog_index.sql": {
        "tray": _HISTORICAL_MIGRATION + " (DEFAULT 'tray_search' nao e usado: o codigo sempre grava factual_source)",
        "newstore": _HISTORICAL_MIGRATION + " (restaurada ao conteudo publicado; a 026 alinha o default de tenant)",
    },
    "sql/024_ai_catalog_index_neutral_factual_source_default.sql": {
        "tray": "migration NOVA que substitui o default legado; cita o valor antigo so no contexto e no rollback",
    },
    "docs/legacy_commerce_values_migration.md": {
        "tray": "plano explicito de migracao dos valores legados gravados e de remocao da leitura compativel",
    },
    "README.md": {
        "nsagent": "somente o NOME DE ARQUIVO do link para a analise comparativa; o texto do link e neutro",
    },
    "docs/nsagent_xnamai_customer_registration_analysis.md": {
        "nsagent": "analise comparativa do cadastro de clientes com o projeto de referencia (veio da main)",
    },
    "docs/xnamai_catalog_club_audit.md": {
        "nsagent": "auditoria de catalogo/Club que registra o que NAO foi herdado do projeto de referencia",
    },
    "docs/architecture_evolution.md": {
        term: "comparacao arquitetural explicita com o projeto de referencia; lista o que NAO e portado"
        for term in ("newstore", "nsagent", "rifa", "sorteio", "tray")
    },
    # --- documentacao historica de migracao ---
    "docs/agent_generative_migration_etapa1.md": {"tray": _HISTORICAL_DOC, "raffle": _HISTORICAL_DOC},
    "docs/agent_generative_migration_etapa4.md": {"tray": _HISTORICAL_DOC},
    "docs/agent_generative_migration_etapa5.md": {"tray": _HISTORICAL_DOC},
    "docs/agent_generative_migration_etapa11.md": {"tray": _HISTORICAL_DOC},
    "docs/agent_optimization_baseline.md": {"tray": _HISTORICAL_DOC, "raffle": _HISTORICAL_DOC},
    "docs/observability_breaking_rename.md": {"tray": _HISTORICAL_DOC},
    "docs/plano_evolucao_omnichannel_instagram.md": {"tray": _HISTORICAL_DOC},
    "docs/v6_architecture_corrections.md": {"tray": _HISTORICAL_DOC},
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _is_scanned(path: pathlib.Path) -> bool:
    rel = path.relative_to(REPO_ROOT)
    if any(part in EXCLUDED_DIR_NAMES for part in rel.parts[:-1]):
        return False
    if path.name in EXCLUDED_FILE_NAMES:
        return False
    return path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES


def _scanned_files() -> list[pathlib.Path]:
    return sorted(
        (path for path in REPO_ROOT.rglob("*") if path.is_file() and _is_scanned(path)),
        key=str,
    )


def find_terms(text: str) -> set[str]:
    folded = _fold(text)
    return {name for name, pattern in FORBIDDEN.items() if pattern.search(folded)}


def _hits_by_file() -> dict[str, set[str]]:
    hits: dict[str, set[str]] = {}
    for path in SCANNED_FILES:
        # Nunca pular arquivo por encoding: um byte invalido nao pode esconder residuo.
        text = path.read_text(encoding="utf-8", errors="replace")
        found = find_terms(text)
        if found:
            hits[path.relative_to(REPO_ROOT).as_posix()] = found
    return hits


SCANNED_FILES = _scanned_files()


def test_scan_is_not_empty():
    """Sem isto a guarda inteira pode passar a vacuo fora da raiz do repo."""
    assert len(SCANNED_FILES) > 100, f"varredura coletou apenas {len(SCANNED_FILES)} arquivos"
    rel = {path.relative_to(REPO_ROOT).as_posix() for path in SCANNED_FILES}
    for required in (
        "app/openai_agent.py",
        "app/prompt_compiler.py",
        "app/config.py",
        "api/index.py",
        ".env.example",
        "persona_xnamai.txt",
        "README.md",
        "vercel.json",
    ):
        assert required in rel, f"{required} ficou fora da varredura"
    assert any(item.startswith("sql/") for item in rel)
    assert not any(item.startswith("tests/") for item in rel)


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("Voce e o NewStoreAgent", {"newstore"}),
        ("AGENT_PERSONA_KEY=newstore_commercial", {"newstore"}),
        ("class NSAgentStoryMedia: ...", {"nsagent"}),
        ("regras do Sorteio", {"sorteio"}),
        ("compre sua rifa", {"rifa"}),
        ("domain='raffle'", {"raffle"}),
        ("resultado da Lotomania", {"lotomania"}),
        ("saldo do Cartão Presente", {"cartao_presente"}),
        ("gift-card", {"gift_card"}),
        ("from app import tray_tools", {"tray"}),
        ('"_factual_source": "tray_live"', {"tray"}),
        ("TrayAdapter", {"tray"}),
        # falsos positivos que o gate NAO pode acusar
        ("verifica o pedido", set()),
        ("portray the product", set()),
        ("went astray", set()),
    ],
)
def test_detector_catches_every_form(sample, expected):
    assert find_terms(sample) == expected


def test_allowlist_entries_are_justified():
    for path, terms in ALLOWLIST.items():
        assert terms, f"{path}: excecao sem termos"
        for term, reason in terms.items():
            assert term in FORBIDDEN, f"{path}: termo desconhecido {term}"
            assert len(reason.strip()) >= 30, f"{path}/{term}: justificativa insuficiente"


def test_repository_has_no_unapproved_legacy_residue():
    offenders = []
    for path, found in sorted(_hits_by_file().items()):
        unapproved = found - set(ALLOWLIST.get(path, {}))
        if unapproved:
            offenders.append(f"{path}: {sorted(unapproved)}")
    assert not offenders, "residuo do produto anterior:\n" + "\n".join(offenders)


def test_allowlist_has_no_stale_entries():
    """Excecao sem ocorrencia correspondente precisa sair da lista."""
    hits = _hits_by_file()
    stale = [
        f"{path}: {term}"
        for path, terms in ALLOWLIST.items()
        for term in terms
        if term not in hits.get(path, set())
    ]
    assert not stale, "excecoes obsoletas no ALLOWLIST:\n" + "\n".join(stale)


def test_rendered_prompt_surface_has_no_legacy_residue():
    """Prompts montados dinamicamente tambem nao podem carregar o dominio antigo."""
    from prompt_surface import render_prompt_surface

    offenders = {
        block: sorted(found)
        for block, text in render_prompt_surface().items()
        if (found := find_terms(text))
    }
    assert not offenders, f"termos legados no texto enviado ao modelo: {offenders}"


def test_runtime_defaults_do_not_select_a_legacy_tenant_or_persona():
    from app.config import Settings

    fields = Settings.model_fields
    for name in ("agent_persona_tenant_id", "agent_persona_key", "commerce_tenant_id"):
        default = str(fields[name].default)
        assert not find_terms(default), f"{name} default legado: {default}"


def test_learning_never_turns_into_active_persona_by_default():
    """learning -> insight -> proposta -> revisao humana; nunca ativacao automatica."""
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["agent_learning_auto_promote"].default is False
    assert fields["agent_learning_auto_activate"].default is False
