"""Conhecimento institucional/comercial oficial da marca.

Parte 1 da migração XNamai: o conteúdo legado (URLs, contatos de suporte,
tabela de crédito e pressupostos de catálogo) foi removido do runtime. Não há
base oficial configurada, e nada aqui deve ser inventado: as constantes ficam
vazias e os construtores de texto declaram explicitamente "não configurado".

As assinaturas e os nomes exportados são preservados — os consumidores
(`agent_replies`, `guardrails`, `handoff_service`, `openai_agent`,
`response_critique`, `simulation`) continuam importando os mesmos símbolos.
"""

from __future__ import annotations

#: Sem site institucional configurado.
SITE_URL = ""
#: Sem loja configurada.
STORE_URL = ""
#: Sem canal de vendas/atendimento humano configurado.
NS_SALES_WHATSAPP = ""

NOT_CONFIGURED_NOTICE = "Base de conhecimento oficial não configurada."

HUMAN_SUPPORT_MESSAGE = (
    "Canal de atendimento humano ainda não configurado. "
    "Não é possível encaminhar um contato agora."
)

TRADE_IN_HANDOFF_MESSAGE = (
    "Política de avaliação, troca ou compra de usados não configurada. "
    "Não posso confirmar nem negar essa política sem base oficial."
)

REGISTER_PHONE_MESSAGE = (
    "Não encontramos telefone cadastrado na sua conta. "
    "Cadastro de telefone não configurado neste canal."
)

# Regra genérica de privacidade (não é específica de marca): só respondemos
# sobre o telefone da própria conversa. Preservada intencionalmente.
THIRD_PARTY_REFUSAL = (
    "Por segurança, só consulto dados do telefone desta conversa. "
    "Não é possível consultar dados de outras pessoas pelo WhatsApp."
)

#: Tabela de crédito (faixa mínima, faixa máxima, compra mínima) em centavos.
#: Vazia: nenhuma tabela oficial configurada.
CARD_USAGE_TABLE: tuple[tuple[int, int, int], ...] = ()


def min_purchase_for_credit_cents(credit_cents: int) -> int | None:
    for low, high, min_purchase in CARD_USAGE_TABLE:
        if low <= credit_cents <= high:
            return min_purchase
    return None


def credit_band_for_amount(credit_cents: int) -> tuple[int, int, int] | None:
    for band in CARD_USAGE_TABLE:
        low, high, _min_purchase = band
        if low <= credit_cents <= high:
            return band
    return None


def max_applicable_credit_for_product_cents(product_cents: int) -> int:
    """Máximo de crédito aplicável dado o valor do produto.

    A guarda de valor não-positivo é genérica (vale para qualquer marca) e é
    preservada de propósito: produto sem valor nunca aceita crédito.
    """
    if product_cents <= 0:
        return 0

    max_credit = 0
    for _low, high, min_purchase in CARD_USAGE_TABLE:
        if product_cents > min_purchase:
            max_credit = max(max_credit, high)
    return max_credit


def format_card_usage_table_text() -> str:
    if not CARD_USAGE_TABLE:
        return "Tabela de utilização de crédito não configurada."

    from .repository import format_cents_to_brl

    lines = ["Tabela para utilização do crédito (referência):"]
    for low, high, min_purchase in CARD_USAGE_TABLE:
        lines.append(
            f"- {format_cents_to_brl(low)} a {format_cents_to_brl(high)} → "
            f"compra deve ser > {format_cents_to_brl(min_purchase)}"
        )
    return "\n".join(lines)


def build_site_knowledge_text() -> str:
    return (
        f"{NOT_CONFIGURED_NOTICE}\n"
        "Não há site, catálogo, política comercial, regulamento ou canal de "
        "atendimento oficial carregado para esta marca.\n"
        "Nunca inventar produto, preço, estoque, prazo, política ou contato: "
        "na ausência de fonte oficial, dizer que a informação não está disponível."
    )


def build_rules_reply() -> str:
    return (
        "Regulamento não configurado. Ainda não tenho a base oficial de regras "
        "para consultar, então não posso confirmar condições."
    )


def build_simulation_reply(credit_cents: int, product_cents: int | None = None) -> str:
    from .simulation import build_purchase_simulation_reply

    return build_purchase_simulation_reply(credit_cents, product_cents=product_cents)
