"""Sinal interno de escopo pessoal — usado SOMENTE pela guarda de privacidade.

Por que este modulo existe
--------------------------
No baseline ``201bd16`` a guarda ``_third_party_guardrail`` so era avaliada
quando o intent primario pertencia ao dominio pessoal (``balance``,
``coupon_code``, ``raffle_history``, ``simulation``). Esses intents eram
produzidos pelos detectores de ``guardrails.py`` e alimentavam TAMBEM os
handlers de feature (saldo, cupom, sorteio, simulacao).

A Parte 1 removeu aquelas features do runtime. Com elas foram os intents — e a
guarda de privacidade ficaria MORTA: ``"saldo do Joao"`` deixaria de ser
recusado. Disparar a guarda para qualquer mensagem, por outro lado, a deixaria
MAIS ESTRITA que o baseline: ``"voces tem MarcaA para o 4899...?"`` viraria
recusa de seguranca.

Este modulo isola o SINAL de classificacao do baseline, sem nada mais:

    mensagem -> is_personal_account_scope() -> _third_party_guardrail

Nao existe — e nao pode passar a existir — o caminho::

    sinal pessoal -> handler de feature -> dados pessoais protegidos

Invariantes (verificados em ``tests/test_third_party_privacy_parity.py``)
------------------------------------------------------------------------
* texto puro: este modulo NAO importa nada;
* nenhum acesso a repositorio, banco, tools ou rede;
* consumido exclusivamente por ``app/openai_agent.py``;
* nao produz intent, rota, resposta nem capability.

Os termos abaixo sao copia literal das listas do baseline. Alguns citam sorteio
e Cartao Presente: aqui eles sao APENAS vocabulario de deteccao para decidir se
a pergunta e sobre a conta de alguem. Nao ativam feature alguma.
"""

from __future__ import annotations

#: ``BALANCE_KEYWORDS`` do baseline.
_BALANCE_TERMS: tuple[str, ...] = (
    "saldo",
    "meu saldo",
    "consultar saldo",
    "ver saldo",
    "quanto tenho",
    "valor do cupom",
)

#: ``COUPON_CODE_KEYWORDS`` do baseline.
_COUPON_TERMS: tuple[str, ...] = (
    "codigo do cupom",
    "código do cupom",
    "numero do cupom",
    "número do cupom",
    "meu cupom",
    "codigo do cartao",
    "código do cartão",
    "cartao presente",
    "cartão presente",
)

#: ``RAFFLE_HISTORY_KEYWORDS`` do baseline — historico de participacao e um dado
#: de CONTA, por isso conta como escopo pessoal.
_PARTICIPATION_TERMS: tuple[str, ...] = (
    "sorteios passados",
    "sorteio passado",
    "ultimo sorteio",
    "último sorteio",
    "ultima participacao",
    "última participação",
    "vencedor",
    "numero sorteado",
    "número sorteado",
    "meus numeros",
    "meus números",
    "participei",
    "participação",
    "participacao",
    "resultado do sorteio",
    "sorteios que participei",
    "sorteio que participei",
)

#: ``SIMULATION_KEYWORDS`` do baseline.
_SIMULATION_TERMS: tuple[str, ...] = (
    "simular",
    "simulação",
    "simulacao",
    "quanto posso usar",
    "tabela de uso",
    "simulador",
)

#: ``PURCHASE_SIMULATION_PHRASES`` do baseline.
_SIMULATION_PHRASES: tuple[str, ...] = (
    "quanto abateria", "quanto abate", "quanto abater", "consigo comprar",
    "consigo usar", "quanto fica", "valor a pagar", "quanto eu pago",
    "quanto pagaria", "usar meu saldo", "usar o saldo", "usar meu cartão",
    "usar meu cartao", "usar o cartão", "usar o cartao", "aplicar o cartão",
    "aplicar o cartao", "aplicar meu cartão", "aplicar meu cartao",
    "abater do valor", "abateria do valor", "abater todo o saldo",
    "abater o saldo", "desconto do cartão", "desconto do cartao",
    "quanto desconta", "quanto descontaria",
)

#: Sinais de credito e de produto de ``detect_purchase_simulation_inquiry``.
#: IMPORTANTE: no baseline eles so contavam EM CONJUNCAO — credito isolado
#: ("cupom", "credito") NAO colocava a mensagem em escopo pessoal. Trata-los
#: como termo solto deixaria a guarda mais estrita que o baseline: "qual o
#: cupom do Pedro" viraria recusa de seguranca onde o baseline seguia adiante.
_CREDIT_SIGNALS: tuple[str, ...] = (
    "saldo", "cartão presente", "cartao presente", "crédito", "credito", "cupom",
)
_PRODUCT_SIGNALS: tuple[str, ...] = (
    "produto", "celular", "fone", "compra", "acessório", "mil", "r$",
)

#: Termos que, sozinhos, ja colocam a mensagem em escopo de conta pessoal.
_PERSONAL_TERMS: tuple[str, ...] = (
    _BALANCE_TERMS + _COUPON_TERMS + _PARTICIPATION_TERMS + _SIMULATION_TERMS
)


def is_personal_account_scope(text: str | None) -> bool:
    """A mensagem fala sobre a conta/credito de alguem?

    Unico uso legitimo: decidir se ``_third_party_guardrail`` deve avaliar a
    mensagem. Devolver ``True`` NAO habilita feature, rota ou consulta — apenas
    coloca a mensagem no escopo em que a recusa de consulta a terceiro se
    aplica, exatamente como no baseline ``201bd16``.
    """
    normalized = (text or "").casefold()
    if not normalized:
        return False
    if any(term in normalized for term in _PERSONAL_TERMS):
        return True
    # Ramo de simulacao do baseline: frase de simulacao, OU credito + produto
    # juntos. Nunca credito isolado.
    if any(phrase in normalized for phrase in _SIMULATION_PHRASES):
        return True
    has_credit = any(signal in normalized for signal in _CREDIT_SIGNALS)
    has_product = any(signal in normalized for signal in _PRODUCT_SIGNALS)
    return has_credit and has_product
