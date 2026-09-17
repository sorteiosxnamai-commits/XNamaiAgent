"""Identidade e canais públicos da Xnamai, sem condições comerciais presumidas."""

SITE_URL = "https://www.xnamai.com/"
STORE_URL = "https://xnamai.meuspedidos.com.br/"

HUMAN_SUPPORT_MESSAGE = (
    "Vou encaminhar seu atendimento à equipe da Xnamai. "
    f"Você também pode acessar os canais oficiais pelo site {SITE_URL}."
)
TRADE_IN_HANDOFF_MESSAGE = (
    "Vou encaminhar sua solicitação à equipe da Xnamai para confirmar se podemos atender."
)
THIRD_PARTY_REFUSAL = (
    "Por segurança, não consulto nem compartilho dados de outras pessoas. "
    "Posso ajudar com o seu atendimento na Xnamai."
)


def build_site_knowledge_text() -> str:
    return f"""Informações institucionais oficiais da Xnamai:
- A Xnamai é uma distribuidora de eletrônicos e acessórios de celular para venda no atacado.
- Site institucional: {SITE_URL}
- Catálogo e portal de pedidos: {STORE_URL}
- Esses endereços são canais oficiais públicos e podem ser compartilhados no atendimento.
- Preço, estoque, compatibilidade, pedido mínimo, frete, pagamento e prazos dependem de confirmação atual no catálogo, nas ferramentas ou com a equipe.
- Não presuma políticas de compra de usados, avaliação, troca, descontos ou promoções.
- Não invente telefone, endereço ou contato. Quando necessário, encaminhe à equipe pelos canais oficiais.
""".strip()
