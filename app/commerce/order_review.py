"""Revisao do pedido e digital do carrinho — deterministicas, sem I/O.

A revisao existe para que o cliente veja exatamente o que sera criado antes de
qualquer coisa ser criada. Por isso ela e pura: mesmos itens, mesmo resultado,
sem rede, sem banco, sem produto.

A digital (`order_fingerprint`) serve a idempotencia que vem depois. Ela
NAO inclui timestamp de proposito: com produto dentro, o mesmo pedido teria
digital nova a cada segundo, e a protecao contra duplicata que ela sustenta iria
junto — um "confirmo" reenviado pelo WhatsApp criaria o segundo pedido.

Nenhum campo e inventado. Sem preco, sem cliente ou sem condicao de pagamento, a
revisao fica explicitamente incompleta e diz o que falta; ela nunca completa a
lacuna por conta propria, porque o resultado apareceria no ERP do cliente.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: Estados possiveis de uma revisao.
REVIEW_EMPTY = "empty_cart"
REVIEW_CUSTOMER_REQUIRED = "customer_resolution_required"
REVIEW_INCOMPLETE = "incomplete"
REVIEW_READY = "ready_for_submission"


@dataclass
class OrderReviewLine:
    """Uma linha da revisao. `subtotal` so existe quando ha preco factual."""

    product_id: str
    name: str | None
    reference: str | None
    quantity: int
    unit_price: float | None
    subtotal: float | None
    stock: int | None = None
    stock_confirmed: bool | None = None


@dataclass
class OrderReview:
    """O que sera enviado, e o que ainda falta para poder enviar."""

    status: str
    lines: list[OrderReviewLine] = field(default_factory=list)
    total: float | None = None
    customer_id: str | None = None
    payment_condition_id: str | None = None
    missing: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    payment_condition_name: str | None = None
    #: Candidatas quando ha mais de uma ativa. Quem escolhe e o cliente.
    payment_condition_options: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == REVIEW_READY


#: Fatos resolvidos por produto: {product_id: {price, stock, stock_confirmed,
#: name, reference}}. Fica FORA do item do carrinho de proposito — aquele modelo
#: e serializado e comparado em outros fluxos, e engorda-lo mudaria um contrato
#: compartilhado por causa de uma necessidade local da revisao.
CartFacts = dict


def _fato(facts: Any, item: Any, campo: str) -> Any:
    if not isinstance(facts, dict):
        return None
    dados = facts.get(str(getattr(item, "product_id", "") or ""))
    return dados.get(campo) if isinstance(dados, dict) else None


def _preco(item: Any, facts: Any = None) -> float | None:
    resolvido = _fato(facts, item, "price")
    if resolvido is not None:
        return float(resolvido)
    if isinstance(facts, dict) and str(getattr(item, "product_id", "")) in facts:
        # Hidratado e sem preco: o indice nao tem o fato. Nao cair para o valor
        # da conversa, que e justamente o que nao se quer usar como verdade.
        return None
    bruto = getattr(item, "unit_price", None)
    if bruto in (None, ""):
        return None
    try:
        return float(str(bruto).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _itens(state) -> list[Any]:
    return list(getattr(state, "cart_items", None) or [])


def _customer_id(state) -> str | None:
    bruto = getattr(state, "mercos_customer_id", None)
    texto = str(bruto).strip() if bruto else ""
    return texto or None


def order_fingerprint(state, *, payment_condition_id: str | None = None,
                      order_type_id: str | None = None,
                      facts: Any = None) -> str:
    """Digital estavel do pedido. Sem produto, sem ordem acidental.

    Os itens sao ordenados por id antes do hash: o mesmo pedido montado em outra
    sequencia continua sendo o mesmo pedido, e duas confirmacoes dele nao podem
    virar dois pedidos.
    """
    itens = sorted(
        (
            {
                "product_id": str(getattr(i, "product_id", "")),
                "quantity": int(getattr(i, "quantity", 0) or 0),
                # Preco RESOLVIDO quando a revisao ja hidratou; senao o que o
                # carrinho tinha. E o preco que vai ao pedido que precisa estar
                # na digital — mudanca de preco tem de invalidar a confirmacao.
                # Preco RESOLVIDO quando a revisao ja hidratou. E o preco que
                # vai ao pedido que precisa estar na digital: mudanca de preco
                # tem de invalidar a confirmacao anterior.
                "unit_price": _preco(i, facts),
            }
            for i in _itens(state)
        ),
        key=lambda linha: linha["product_id"],
    )
    corpo = {
        "customer_id": _customer_id(state),
        "payment_condition_id": (payment_condition_id or None),
        "order_type_id": (order_type_id or None),
        "items": itens,
    }
    serializado = json.dumps(corpo, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serializado.encode("utf-8")).hexdigest()


def build_order_review(
    state,
    *,
    payment_condition_id: str | None = None,
    order_type_id: str | None = None,
    facts: Any = None,
) -> OrderReview:
    """Monta a revisao a partir do carrinho da conversa.

    A ordem das checagens e a ordem em que o cliente consegue resolver: carrinho
    vazio primeiro, depois cliente, depois o resto. Listar tudo de uma vez faria
    a resposta pedir dados que ainda nem fazem sentido.
    """
    itens = _itens(state)
    faltando: list[str] = []
    nao_confirmado: list[str] = []

    linhas: list[OrderReviewLine] = []
    total: float | None = 0.0
    for item in itens:
        preco = _preco(item, facts)
        quantidade = int(getattr(item, "quantity", 0) or 0)
        subtotal = preco * quantidade if preco is not None else None
        if subtotal is None:
            total = None
        elif total is not None:
            total += subtotal
        linhas.append(
            OrderReviewLine(
                product_id=str(getattr(item, "product_id", "")),
                name=_fato(facts, item, "name") or getattr(item, "name", None),
                reference=_fato(facts, item, "reference"),
                quantity=quantidade,
                unit_price=preco,
                subtotal=subtotal,
                stock=_fato(facts, item, "stock"),
                stock_confirmed=_fato(facts, item, "stock_confirmed"),
            )
        )

    customer_id = _customer_id(state)
    condicao = (payment_condition_id or "").strip() or None
    digital = order_fingerprint(
        state, payment_condition_id=condicao, order_type_id=order_type_id, facts=facts
    )

    if not itens:
        return OrderReview(
            status=REVIEW_EMPTY, lines=[], total=None,
            customer_id=customer_id, payment_condition_id=condicao,
            missing=["itens"], fingerprint=digital,
        )

    if any(linha.unit_price is None for linha in linhas):
        faltando.append("preco_unitario")
    if any(linha.quantity < 1 for linha in linhas):
        faltando.append("quantidade")
    if condicao is None:
        faltando.append("payment_condition")

    # As regras de estoque so fazem sentido depois da hidratacao: sem ela, o
    # desconhecido e ausencia de consulta, nao ausencia de peca.
    for linha in (linhas if isinstance(facts, dict) else []):
        if linha.stock is None:
            # Ausencia nao e zero: o item pode existir e a fonte so nao ter
            # confirmado. Nao bloqueia como "acabou", mas tambem nao deixa a
            # revisao afirmar disponibilidade.
            if "estoque_nao_confirmado" not in nao_confirmado:
                nao_confirmado.append("estoque_nao_confirmado")
        elif linha.stock < linha.quantity:
            if "estoque_insuficiente" not in faltando:
                faltando.append("estoque_insuficiente")
        elif linha.stock_confirmed is False:
            if "estoque_nao_confirmado" not in nao_confirmado:
                nao_confirmado.append("estoque_nao_confirmado")

    if customer_id is None:
        # Sem cliente o pedido nao existe. Reportado como estado proprio para
        # que o fluxo peca o cadastro em vez de listar campos soltos.
        return OrderReview(
            status=REVIEW_CUSTOMER_REQUIRED, lines=linhas, total=total,
            customer_id=None, payment_condition_id=condicao,
            missing=["customer_id", *faltando], unconfirmed=nao_confirmado,
            fingerprint=digital,
        )

    # Estoque desconhecido nao e falta de dado do pedido, mas tambem nao
    # autoriza fechar: prometer entrega sem saber se ha peca e o erro que o
    # cliente so descobre depois.
    status = REVIEW_READY if not faltando and not nao_confirmado else REVIEW_INCOMPLETE
    return OrderReview(
        status=status, lines=linhas, total=total,
        customer_id=customer_id, payment_condition_id=condicao,
        missing=faltando, unconfirmed=nao_confirmado, fingerprint=digital,
    )


# === hidratacao: os fatos vem do indice local, nao da conversa ==============


async def hydrate_cart_facts(state, *, execute) -> tuple[dict, dict[str, int]]:
    """Fatos atuais de cada produto do carrinho, vindos do INDICE LOCAL.

    Acontece na REVISAO, e nao ao adicionar, por dois motivos. Adicionar nao
    pode custar consulta — o carrinho e a parte da conversa que precisa ser
    instantanea. E o preco que vale e o do momento em que o pedido e montado,
    nao o que foi mencionado dez mensagens atras.

    `execute` e o mesmo despachante de tools do resto do sistema, e as duas
    capacidades usadas (`get_product`, `check_inventory`) leem o indice local:
    nenhuma chamada sai para o adaptador, que serializa e dorme 2s por request.

    Cada produto e consultado uma unica vez, mesmo repetido no carrinho.
    """
    identificadores: list[str] = []
    for item in _itens(state):
        pid = str(getattr(item, "product_id", "") or "")
        if pid and pid not in identificadores:
            identificadores.append(pid)

    contagem = {"product_lookups": 0, "inventory_lookups": 0}
    fatos: dict[str, dict[str, Any]] = {}
    for pid in identificadores:
        detalhe = await execute("get_product", {"product_id": pid})
        contagem["product_lookups"] += 1
        produto = detalhe.get("product") if detalhe.get("ok") else None
        produto = produto if isinstance(produto, dict) else {}

        estoque = await execute("check_inventory", {"product_id": pid})
        contagem["inventory_lookups"] += 1
        tem_estoque = estoque.get("ok") is True

        preco = produto.get("price")
        fatos[pid] = {
            "price": None if preco is None else float(preco),
            "name": produto.get("name"),
            "reference": produto.get("reference"),
            "stock": estoque.get("stock") if tem_estoque else None,
            "stock_confirmed": estoque.get("stock_confirmed") if tem_estoque else None,
        }
    return fatos, contagem


async def build_hydrated_order_review(
    state,
    *,
    execute,
    payment_condition_id: str | None = None,
    order_type_id: str | None = None,
) -> OrderReview:
    """Resolve os fatos do carrinho e so entao monta a revisao."""
    fatos, _ = await hydrate_cart_facts(state, execute=execute)
    return build_order_review(
        state,
        payment_condition_id=payment_condition_id,
        order_type_id=order_type_id,
        facts=fatos,
    )


async def run_order_review(
    state,
    *,
    execute,
    cache=None,
    fetch_conditions,
    now: float | None = None,
) -> OrderReview:
    """A revisao completa: resolve TUDO e deixa o pedido pendente de confirmacao.

    A ordem e a parte importante. Produto, estoque e condicao de pagamento sao
    resolvidos AQUI, antes da digital — resolver a condicao no "confirmo" faria o
    cliente autorizar um pedido e o sistema ir buscar, depois disso, um dado
    comercial que pode ter mudado. O prazo que ele viu na revisao nao seria
    necessariamente o prazo do pedido criado.

    Depois disso a confirmacao nao consulta nada: ela apenas diz "sim" para esta
    revisao. Qualquer mudanca — item, quantidade, preco, condicao — produz outra
    digital, e a confirmacao anterior deixa de servir.
    """
    from .payment_conditions import (
        PAYMENT_CONDITION_REQUIRED,
        PAYMENT_CONDITION_SELECTED,
        PAYMENT_CONDITION_SELECTION_REQUIRED,
        PAYMENT_CONDITION_UNAVAILABLE,
        resolve_payment_condition,
    )

    fatos, _ = await hydrate_cart_facts(state, execute=execute)

    condicao = await resolve_payment_condition(
        cache=cache, fetch=fetch_conditions, now=now
    )
    if condicao.status == PAYMENT_CONDITION_SELECTED:
        state.mercos_payment_condition_id = condicao.condition_id
        state.mercos_payment_condition_name = condicao.condition_name
    else:
        # Sem condicao confirmada, o estado nao guarda uma escolha antiga: ela
        # poderia ter sido descontinuada, e um prazo extinto fecharia o pedido.
        state.mercos_payment_condition_id = None
        state.mercos_payment_condition_name = None

    revisao = build_order_review(
        state,
        payment_condition_id=state.mercos_payment_condition_id,
        facts=fatos,
    )
    revisao.payment_condition_name = state.mercos_payment_condition_name
    revisao.payment_condition_options = list(condicao.options)

    if condicao.status != PAYMENT_CONDITION_SELECTED:
        # O motivo entra como ele e: "nenhuma cadastrada", "escolha necessaria" e
        # "nao consegui confirmar" levam a conversas diferentes.
        motivo = {
            PAYMENT_CONDITION_REQUIRED: PAYMENT_CONDITION_REQUIRED,
            PAYMENT_CONDITION_SELECTION_REQUIRED: PAYMENT_CONDITION_SELECTION_REQUIRED,
            PAYMENT_CONDITION_UNAVAILABLE: PAYMENT_CONDITION_UNAVAILABLE,
        }.get(condicao.status, PAYMENT_CONDITION_REQUIRED)
        if motivo not in revisao.missing:
            revisao.missing.append(motivo)
        if revisao.status == REVIEW_READY:
            revisao.status = REVIEW_INCOMPLETE

    state.order_review_version = revisao.fingerprint
    state.order_confirmation_status = (
        "pending" if revisao.status == REVIEW_READY else "not_ready"
    )
    return revisao
