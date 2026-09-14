from __future__ import annotations

SITE_URL = "https://www.sorteionewstore.com.br/"
STORE_URL = "https://www.newstorerj.com.br/"
NS_SALES_WHATSAPP = "+55 48 9949-0859"

HUMAN_SUPPORT_MESSAGE = (
    f"Para falar com a equipe da New Store (vendas e dúvidas), chame no WhatsApp {NS_SALES_WHATSAPP}. "
    "Lá você encontra orientação e atendimento humano."
)

TRADE_IN_HANDOFF_MESSAGE = (
    "A New Store avalia, troca e compra relógios. "
    "Vou encaminhar você para um atendente da loja seguir com a avaliação. "
    f"{HUMAN_SUPPORT_MESSAGE}"
)

REGISTER_PHONE_MESSAGE = (
    "Não encontramos telefone cadastrado na sua conta. "
    f"Acesse {SITE_URL}, faça login e inclua seu telefone no perfil. "
    "Use o mesmo número deste WhatsApp para consultar saldo, cupom e participações com segurança."
)

THIRD_PARTY_REFUSAL = (
    "Por segurança, só consulto saldo, cupom e participações do telefone desta conversa. "
    "Não é possível consultar dados de outras pessoas pelo WhatsApp."
)

# Tabela de referência do site (cartão presente x valor mínimo de compra), em centavos.
CARD_USAGE_TABLE = (
    (50_00, 250_00, 150_000),
    (251_00, 600_00, 350_000),
    (601_00, 800_00, 550_000),
    (801_00, 1_000_00, 750_000),
    (1_101_00, 2_100_00, 1_500_000),
    (2_101_00, 3_100_00, 2_250_000),
    (3_101_00, 4_200_00, 3_000_000),
)


def min_purchase_for_credit_cents(credit_cents: int) -> int | None:
    for low, high, min_purchase in CARD_USAGE_TABLE:
        if low <= credit_cents <= high:
            return min_purchase
    return None


def credit_band_for_amount(credit_cents: int) -> tuple[int, int, int] | None:
    for band in CARD_USAGE_TABLE:
        low, high, min_purchase = band
        if low <= credit_cents <= high:
            return band
    return None


def max_applicable_credit_for_product_cents(product_cents: int) -> int:
    """Máximo de cartão aplicável dado o valor do produto (tabela oficial)."""
    if product_cents <= 0:
        return 0

    max_credit = 0
    for low, high, min_purchase in CARD_USAGE_TABLE:
        if product_cents > min_purchase:
            max_credit = max(max_credit, high)
    return max_credit


def format_card_usage_table_text() -> str:
    from .repository import format_cents_to_brl

    lines = ["Tabela para utilização do Cartão Presente (referência):"]
    for low, high, min_purchase in CARD_USAGE_TABLE:
        lines.append(
            f"- {format_cents_to_brl(low)} a {format_cents_to_brl(high)} → compra deve ser > {format_cents_to_brl(min_purchase)}"
        )
    lines.append(
        "A tabela é referência; o simulador desconta o valor aplicado respeitando o teto por faixa. "
        "O valor do produto define quanto de cartão pode ser aplicado; o saldo disponível pode ser maior "
        "do que o permitido naquela compra. Sempre considerar o valor integral do produto na forma de pagamento escolhida (Pix ou crédito)."
    )
    return "\n".join(lines)


def build_site_knowledge_text() -> str:
    return f"""
Base oficial New Store Sorteios ({SITE_URL}):

Proposta:
- Sorteio em que você concorre a prêmios e recebe 100% do valor investido de volta em Cartão Presente Digital.
- Sorteio válido até preencher a tabela, com base no resultado oficial da Lotomania (Caixa).

Regras do sorteio:
- A vaga só é confirmada após a compensação do pagamento.
- O sorteio é realizado assim que todos os números são vendidos.
- O ganhador é o participante com o último número sorteado pela Lotomania.
- Prazo máximo: 7 dias após abertura da rodada.
- Envio do prêmio: frete por conta do vencedor.
- O Cartão Presente não é cumulativo com o prêmio nem com outras promoções do site.
- Transparência total: o resultado pode ser conferido publicamente no site oficial da Caixa Econômica Federal.

Cartão Presente Digital:
- Cada participação gera crédito acumulativo em um único cartão.
- Validade de 6 meses, renovada automaticamente a cada nova participação.
- Uso exclusivo no site da New Store Relógios ({STORE_URL}).
- Código pessoal e intransferível.
- Sem conversão em dinheiro.
- Não é possível comprar outro cartão-presente com crédito de sorteio.
- Utilização em uma única compra; pode usar em mais de um produto na mesma compra e também só parte do saldo acumulado.
- Solicitar orientação no grupo oficial quando precisar usar parte do saldo.
- A New Store não se responsabiliza por perda, extravio ou validade expirada.
- O cartão não é cumulativo com outros cupons de desconto.
- Sempre considerar o valor integral do produto na forma de pagamento escolhida (Pix ou crédito).
- Desconto Pix pode exigir aplicação manual pela equipe da loja.

{format_card_usage_table_text()}

Exemplo prático (referência do site):
- Relógio Tissot PRX Powermatic 80 no crédito: R$ 6.799,99.
- Aplicando R$ 800,00 de Cartão Presente → valor a pagar R$ 5.999,99 (até 12x sem juros).
- À vista no Pix: R$ 5.779,99; com R$ 800,00 de cartão → R$ 4.979,99.
- Importante: o desconto segue a forma de pagamento; compras via Pix podem ter desconto aplicado manualmente pela equipe.

Atendimento humano (vendas e dúvidas): WhatsApp {NS_SALES_WHATSAPP}.

Política comercial New Store Relógios:
- A New Store avalia, troca e compra relógios (incluindo seminovos).
- Pedidos de avaliação, troca ou compra de usado devem ser encaminhados ao atendente humano.
- Nunca diga que a loja não compra ou não avalia relógios.

FAQ resumido:
1. Sorteio baseado na Lotomania; ganhador = último número sorteado.
2. Sorteio após venda de todos os números.
3. Participação gera crédito no site + concorre ao prêmio.
4. Cartão só no site New Store, conforme tabela de utilização.
5. Crédito não transferível.
6. Frete do prêmio não incluso.
7. Resultados e novas rodadas também no grupo oficial WhatsApp.
""".strip()


def build_rules_reply() -> str:
    return (
        "Sorteio New Store: você concorre ao prêmio e recebe 100% do valor investido em Cartão Presente Digital "
        f"para usar em {STORE_URL}. "
        "O sorteio usa a Lotomania (último número sorteado vence). "
        "A vaga confirma após pagamento; o sorteio ocorre quando a tabela enche (prazo máximo 7 dias). "
        "Frete do prêmio por conta do vencedor. "
        "Cartão: validade 6 meses (renovável), pessoal, sem dinheiro, uso exclusivo no site, "
        "não cumulativo com prêmio nem com outros cupons. "
        f"Dúvidas e novas rodadas: {SITE_URL} e grupo oficial WhatsApp. "
        f"Atendimento humano (vendas): {NS_SALES_WHATSAPP}."
    )


def build_simulation_reply(credit_cents: int, product_cents: int | None = None) -> str:
    from .simulation import build_purchase_simulation_reply

    return build_purchase_simulation_reply(credit_cents, product_cents=product_cents)
