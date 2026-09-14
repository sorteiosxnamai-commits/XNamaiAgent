"""O agente e da XNamai — e nada no prompt diz o contrario.

O sintoma que originou estes testes, em producao:

    usuario: "vc e assistente de que?"
    agente : "Posso ajudar com produtos, compras, pedidos e informacoes da
              NewStore, alem dos sorteios da loja."

A causa nao era a persona do banco. Era o codigo: `SYSTEM_INSTRUCTIONS` do
`openai_agent` entra no prompt como `<operational_contract>` MESMO quando a
persona vem do banco (`prompt_compiler.compile_agent_prompt`). Trocar so a
persona do banco deixaria a identidade antiga viva por baixo.

Por isso estes testes olham para o TEXTO COMPILADO, nao para a persona isolada.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Nomes que nao podem definir a identidade atual do agente.
LEGACY_IDENTITY = ("NewStoreAgent", "New Store", "NewStore", "newstore")
LEGACY_PROVIDER = ("TrayAdapter", "Tray")
LEGACY_DOMAIN = ("sorteionewstore", "sorteio", "Sorteio")


def _surface() -> dict[str, str]:
    from prompt_surface import render_prompt_surface

    return render_prompt_surface()


# --- a superficie ativa nao carrega a marca legada -------------------------


@pytest.mark.parametrize("term", LEGACY_IDENTITY + LEGACY_PROVIDER + LEGACY_DOMAIN)
def test_no_legacy_brand_in_the_active_prompt_surface(term):
    ofensores = {b: t.count(term) for b, t in _surface().items() if term in t}
    assert not ofensores, f"{term!r} ainda chega ao modelo em: {sorted(ofensores)}"


def test_system_instructions_declare_the_xnamai_identity():
    from app.openai_agent import SYSTEM_INSTRUCTIONS

    assert "XNamai" in SYSTEM_INSTRUCTIONS
    for term in LEGACY_IDENTITY:
        assert term not in SYSTEM_INSTRUCTIONS


def test_system_instructions_say_history_does_not_override_identity():
    """Mensagem antiga do proprio assistente nao e fonte de verdade."""
    from app.openai_agent import SYSTEM_INSTRUCTIONS

    # Espacos normalizados: a frase quebra linha no prompt, e o que importa e a
    # regra, nao a largura da coluna.
    texto = " ".join(SYSTEM_INSTRUCTIONS.casefold().split())
    assert "identidade" in texto
    assert "não é fonte de verdade" in texto


def test_system_instructions_forbid_announcing_inactive_capabilities():
    from app.openai_agent import SYSTEM_INSTRUCTIONS

    texto = SYSTEM_INSTRUCTIONS.casefold()
    assert "não a anuncie" in texto or "nao a anuncie" in texto


# --- fallbacks: o caminho sem persona de banco -----------------------------


def test_emergency_fallback_identity_is_xnamai():
    """Sem persona no banco E sem fallback de codigo, a identidade e XNamai."""
    import inspect

    from app import prompt_compiler

    fonte = inspect.getsource(prompt_compiler)
    assert "assistente virtual da XNamai" in fonte
    for term in LEGACY_IDENTITY:
        assert term not in fonte, f"prompt_compiler ainda cita {term!r}"


def test_persona_preview_fallback_is_not_legacy():
    """O preview do admin nao pode reintroduzir a identidade antiga."""
    import inspect

    from app import persona_admin_api

    fonte = inspect.getsource(persona_admin_api)
    for term in LEGACY_IDENTITY:
        assert term not in fonte, f"prompt-preview ainda cita {term!r}"


def test_fixed_safety_policy_is_provider_agnostic():
    """A politica de seguranca nao pode nomear fornecedor nem dominio legado."""
    from app.prompt_compiler import FIXED_SAFETY_POLICY

    for term in LEGACY_IDENTITY + LEGACY_PROVIDER + LEGACY_DOMAIN:
        assert term not in FIXED_SAFETY_POLICY
    # E nao pode ter enfraquecido: as garantias continuam la.
    texto = FIXED_SAFETY_POLICY.casefold()
    for garantia in ("nunca invente", "cvv", "isolamento", "credenciais"):
        assert garantia in texto


# --- copy que chega ao cliente ---------------------------------------------


def test_out_of_scope_reply_speaks_for_xnamai():
    """A frase exata que o cliente viu em producao."""
    from app.sales_agent import OUT_OF_SCOPE_REPLY

    assert "XNamai" in OUT_OF_SCOPE_REPLY
    for term in LEGACY_IDENTITY + LEGACY_DOMAIN:
        assert term not in OUT_OF_SCOPE_REPLY


def test_handoff_copy_never_names_the_legacy_company():
    from app.guardrails import default_safe_handoff
    from app.handoff_service import build_human_handoff_result

    textos = [
        default_safe_handoff(),
        build_human_handoff_result(reason="customer_requested_human").reply_text,
        build_human_handoff_result(reason="trade_in_or_appraisal").reply_text,
    ]
    for texto in textos:
        for term in LEGACY_IDENTITY:
            assert term not in texto, f"copy de handoff cita {term!r}: {texto[:60]}"
        assert "XNamai" in texto


def test_handoff_metadata_never_publishes_the_legacy_contact():
    """Publicar o telefone da marca antiga rotearia o cliente para outra empresa."""
    from app.handoff_service import build_human_handoff_result
    from app.site_knowledge import NS_SALES_WHATSAPP

    resultado = build_human_handoff_result(reason="customer_requested_human")
    assert NS_SALES_WHATSAPP not in str(resultado.response_metadata)


# --- site_knowledge: legado protegido, mas fora do prompt ------------------


def test_site_knowledge_file_is_untouched():
    """O arquivo legado nao pode ser editado nesta migracao."""
    import subprocess

    diff = subprocess.run(
        ["git", "diff", "HEAD", "--", "app/site_knowledge.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout
    assert diff.strip() == "", "app/site_knowledge.py foi modificado"


def test_privacy_scope_file_is_untouched():
    import subprocess

    diff = subprocess.run(
        ["git", "diff", "HEAD", "--", "app/privacy_scope.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout
    assert diff.strip() == "", "app/privacy_scope.py foi modificado"


def test_site_knowledge_is_not_injected_into_the_prompt():
    """O conhecimento institucional da marca legada nao chega mais ao modelo."""
    fonte = (REPO_ROOT / "app" / "openai_agent.py").read_text(encoding="utf-8")
    assert "build_site_knowledge_text()" not in fonte

    for bloco, texto in _surface().items():
        assert "Cartão Presente" not in texto, f"{bloco} traz conhecimento legado"
        assert "Lotomania" not in texto, f"{bloco} traz conhecimento legado"


def test_no_prompt_module_still_imports_the_legacy_knowledge_text():
    import ast

    ofensores = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "site_knowledge.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "site_knowledge"
            ):
                nomes = {a.name for a in node.names}
                if "build_site_knowledge_text" in nomes:
                    ofensores.append(path.relative_to(REPO_ROOT).as_posix())
    assert not ofensores, f"conhecimento legado ainda importado por: {ofensores}"


# --- a integracao comercial nao foi tocada ---------------------------------


def test_commerce_behaviour_is_unchanged():
    """Migracao de identidade nao mexe em capability comercial."""
    from app.commerce.mercos.provider import LLM_EXPOSED_CAPABILITIES
    from app.commerce.provider import NullCommerceProvider, get_commerce_provider

    assert LLM_EXPOSED_CAPABILITIES == frozenset(
        {"search_products", "get_product", "check_inventory"}
    )
    assert isinstance(get_commerce_provider(), NullCommerceProvider)
