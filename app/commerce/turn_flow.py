"""Executa o turno comercial: resolve a referencia, busca o fato, guarda o estado.

Divisao de trabalho deste pacote:

* `turn_resolver` decide O QUE o cliente quis dizer e SOBRE QUAL produto — puro,
  so texto e estado;
* aqui as tools sao chamadas e o estado e atualizado;
* a persona apenas verbaliza o que sai daqui.

Duas regras moldam o modulo.

*Nunca reconstruir produto a partir do texto da resposta.* O produto ativo guarda
identidade (id, nome, referencia) e todo follow-up usa o ID — e assim que
"quanto custa?" e "tem foto?" continuam falando do mesmo item.

*Tres ausencias diferentes nao podem virar a mesma frase.* Produto inexistente,
produto sem imagem e catalogo fora do ar sao situacoes distintas; dizer "nao
encontrei esse produto" quando o item existe mas nao tem foto faz o cliente
desistir de algo que estava disponivel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .generic_catalog import MAX_AMBIGUOUS, match_generic_catalog_product, search_queries_for
from .turn_resolver import (
    ACTION_BROWSE_CATALOG,
    ACTION_CONFIRM_PENDING,
    ACTION_REJECT_PENDING,
    ACTION_ADD_TO_CART,
    ACTION_CLEAR_CART,
    ACTION_CONFIRM_ORDER,
    ACTION_REMOVE_FROM_CART,
    ACTION_REVIEW_ORDER,
    ACTION_SET_QUANTITY,
    ACTION_SHOW_CART,
    ACTION_START_PURCHASE,
    LOCAL_CART_ACTIONS,
    PENDING_BROWSE,
    PENDING_CONFIRM_ORDER,
    cart_quantity,
    ACTION_CHECK_INVENTORY,
    ACTION_COMPARE,
    ACTION_CORRECT_REFERENCE,
    ACTION_GET_DETAILS,
    ACTION_GET_PRICE,
    ACTION_REJECT_PRODUCT,
    ACTION_SELECT_PRODUCT,
    ACTION_SHOW_MEDIA,
    ACTION_SHOW_MORE_MEDIA,
    FACT_MEDIA,
    resolve_commerce_turn,
)

OUTCOME_BROWSE = "browse"
OUTCOME_PRODUCT = "product"
OUTCOME_PRICE = "price"
OUTCOME_INVENTORY = "inventory"
OUTCOME_DETAILS = "details"
OUTCOME_MEDIA = "media"
OUTCOME_MEDIA_UNAVAILABLE = "media_unavailable"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_COMPARE = "compare"
OUTCOME_REJECTED = "rejected"
OUTCOME_PRODUCT_NOT_FOUND = "product_not_found"
OUTCOME_PROVIDER_UNAVAILABLE = "provider_unavailable"
OUTCOME_CLARIFICATION = "clarification"
OUTCOME_PURCHASE_INTENT = "purchase_intent"
OUTCOME_PENDING_REJECTED = "pending_rejected"
OUTCOME_CART = "cart"
OUTCOME_ORDER_READY = "order_ready_for_submission"
OUTCOME_ORDER_INCOMPLETE = "order_incomplete"
OUTCOME_ORDER_REVIEW = "order_review"

#: Teto de paginas por mensagem. Varrer o catalogo inteiro a cada turno seria
#: caro e desnecessario: uma amostra responde a pergunta generica.
MAX_BROWSE_PAGES = 4
BROWSE_PAGE_SIZE = 10

#: Campos neutros onde uma imagem pode aparecer. Hoje a fonte comercial nao
#: entrega nenhum deles — o fluxo existe correto e passa a servir imagem sozinho
#: no dia em que ela existir, sem inventar URL nenhuma enquanto isso.
_MEDIA_FIELDS = ("image_url", "imagem_url", "foto_url", "image", "photo")
_MEDIA_LISTS = ("images", "imagens", "fotos", "media")


@dataclass
class CommerceTurnOutcome:
    """Fatos para a persona verbalizar. Sem texto de resposta, sem opiniao."""

    outcome: str
    #: Motivos factuais quando a revisao nao fecha. Cada um leva a uma conversa
    #: diferente — colapsar tudo em "clarification" perderia a informacao.
    missing: list[str] = field(default_factory=list)
    unconfirmed: list[str] = field(default_factory=list)
    review: Any | None = None
    product: dict[str, Any] | None = None
    products: list[dict[str, Any]] = field(default_factory=list)
    inventory: dict[str, Any] | None = None
    media: list[str] = field(default_factory=list)
    only_one_media: bool = False
    rejected_product_id: str | None = None
    action: str | None = None


def product_media_urls(product: dict[str, Any] | None) -> list[str]:
    """Imagens factuais do produto. Vazio quando a fonte nao entrega nenhuma."""
    if not isinstance(product, dict):
        return []
    urls: list[str] = []
    for campo in _MEDIA_LISTS:
        valor = product.get(campo)
        if isinstance(valor, (list, tuple)):
            urls.extend(str(v).strip() for v in valor if isinstance(v, str) and v.strip())
    for campo in _MEDIA_FIELDS:
        valor = product.get(campo)
        if isinstance(valor, str) and valor.strip():
            urls.append(valor.strip())
    vistos: list[str] = []
    for url in urls:
        if url not in vistos:
            vistos.append(url)
    return vistos


def _identidade(produto: dict[str, Any]):
    from ..commerce_context import CommerceProductReference

    return CommerceProductReference(
        product_id=str(produto.get("id") or produto.get("external_id") or ""),
        name=produto.get("name"),
        reference=produto.get("reference"),
        ean=produto.get("ean"),
        brand=produto.get("brand"),
    )


def _ativar(state, produto: dict[str, Any]) -> None:
    anterior = getattr(state, "active_product", None)
    if anterior is not None and anterior.product_id != str(
        produto.get("id") or produto.get("external_id")
    ):
        state.previous_active_product = anterior
    state.active_product = _identidade(produto)


def _guardar_lista(state, produtos: list[dict[str, Any]]) -> None:
    from ..commerce_context import PresentedCommerceProduct

    state.last_presented_products = [
        PresentedCommerceProduct(
            product_id=str(p.get("id") or p.get("external_id") or ""),
            name=p.get("name"),
            reference=p.get("reference"),
            position=posicao,
        )
        for posicao, p in enumerate(produtos, start=1)
        if (p.get("id") or p.get("external_id"))
    ]


async def _browse(execute, quantos: int) -> tuple[list[dict[str, Any]], bool]:
    """Amostra do catalogo, paginando so o necessario para completar `quantos`."""
    reunidos: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for pagina in range(1, MAX_BROWSE_PAGES + 1):
        resultado = await execute(
            "search_products",
            {"limit": BROWSE_PAGE_SIZE, "page": pagina, "available": True},
        )
        if not resultado.get("ok"):
            return [], False
        lote = list(resultado.get("products") or [])
        for produto in lote:
            chave = str(produto.get("id") or produto.get("external_id") or "")
            if chave and chave not in vistos:
                vistos.add(chave)
                reunidos.append(produto)
        if len(reunidos) >= quantos or not lote:
            break
    return reunidos[:quantos], True


async def _buscar_literal(execute, termo: str) -> tuple[list[dict[str, Any]], bool]:
    """Busca o termo EXATAMENTE como veio, sem normalizar.

    Codigo de produto costuma ter pontuacao significativa: "AD-190" com hifen
    casa o nome; "ad 190" sem ele nao casa nada, porque a leitura do indice e um
    LIKE de trecho contiguo.
    """
    resultado = await execute(
        "search_products", {"query": termo, "limit": 10, "page": 1}
    )
    if not resultado.get("ok"):
        return [], False
    return list(resultado.get("products") or []), True


async def _buscar(execute, consulta: str) -> tuple[list[dict[str, Any]], bool]:
    for tentativa in search_queries_for(consulta):
        resultado = await execute(
            "search_products", {"query": tentativa, "limit": 10, "page": 1}
        )
        if not resultado.get("ok"):
            return [], False
        encontrados = list(resultado.get("products") or [])
        if encontrados:
            return encontrados, True
    return [], True


async def _detalhar(execute, product_id: str) -> dict[str, Any] | None:
    detalhe = await execute("get_product", {"product_id": str(product_id)})
    if detalhe.get("ok") and isinstance(detalhe.get("product"), dict):
        return detalhe["product"]
    return None


async def run_commerce_turn(
    text: str, *, state, execute, fetch_conditions=None
) -> CommerceTurnOutcome:
    """Um turno comercial completo, do texto ao fato — atualizando o estado."""
    resolucao = resolve_commerce_turn(text, state=state)
    acao = resolucao.action
    if resolucao.requires_catalog_search and resolucao.product_query:
        state.last_catalog_query = resolucao.product_query

    # --- resposta a uma pergunta que o proprio bot fez ----------------------
    if acao == ACTION_REJECT_PENDING:
        state.pending_commerce_action = None
        state.last_commerce_action = acao
        return CommerceTurnOutcome(outcome=OUTCOME_PENDING_REJECTED, action=acao)
    if acao == ACTION_CONFIRM_PENDING:
        # A pergunta pendente vira a acao a executar. Limpar ANTES evita que um
        # "sim" seguinte reexecute a mesma coisa.
        pendente = getattr(state, "pending_commerce_action", None)
        state.pending_commerce_action = None
        acao = ACTION_BROWSE_CATALOG if pendente == PENDING_BROWSE else acao
        if acao != ACTION_BROWSE_CATALOG:
            state.last_commerce_action = acao
            return CommerceTurnOutcome(outcome=OUTCOME_CLARIFICATION, action=acao)

    # --- carrinho: tudo local, zero chamada externa -------------------------
    if acao == ACTION_REVIEW_ORDER:
        return await _turno_de_revisao(
            state=state, execute=execute, fetch_conditions=fetch_conditions
        )

    if acao in LOCAL_CART_ACTIONS or acao == ACTION_CONFIRM_ORDER:
        return await _turno_de_carrinho(acao, resolucao, state=state, execute=execute)

    # --- intencao de comprar ------------------------------------------------
    if acao == ACTION_START_PURCHASE:
        # Nenhuma promessa de pedido criado: a capability de pedido nao esta
        # exposta. O que da para fazer agora e ajudar a escolher o produto.
        state.last_commerce_action = acao
        state.pending_commerce_action = PENDING_BROWSE
        return CommerceTurnOutcome(
            outcome=OUTCOME_PURCHASE_INTENT,
            product=None,
            action=acao,
        )

    # --- browse -------------------------------------------------------------
    if acao == ACTION_BROWSE_CATALOG:
        from app.business_policy import current_policy
        quantos = current_policy().catalog_browse_limit
        produtos, ok = await _browse(execute, quantos)
        if not ok:
            return CommerceTurnOutcome(outcome=OUTCOME_PROVIDER_UNAVAILABLE, action=acao)
        _guardar_lista(state, produtos)
        # Deliberado: apresentar NAO ativa o primeiro item. O cliente ainda nao
        # escolheu, e ativar por conta propria faria o proximo "quanto custa?"
        # falar de um produto que ele nunca mencionou.
        state.last_commerce_action = acao
        state.last_requested_fact = None
        return CommerceTurnOutcome(outcome=OUTCOME_BROWSE, products=produtos, action=acao)

    # --- rejeicao -----------------------------------------------------------
    if acao == ACTION_REJECT_PRODUCT:
        rejeitado = resolucao.rejected_product_id
        if rejeitado and getattr(state, "active_product", None) is not None:
            state.previous_active_product = state.active_product
            state.active_product = None
        state.last_commerce_action = acao
        if resolucao.product_query:
            encontrados, ok = await _buscar(execute, resolucao.product_query)
            if not ok:
                return CommerceTurnOutcome(
                    outcome=OUTCOME_PROVIDER_UNAVAILABLE, action=acao,
                    rejected_product_id=rejeitado,
                )
            alternativas = [
                p for p in encontrados
                if str(p.get("id") or p.get("external_id")) != str(rejeitado)
            ]
            if alternativas:
                _guardar_lista(state, alternativas[:MAX_AMBIGUOUS])
                return CommerceTurnOutcome(
                    outcome=OUTCOME_BROWSE, products=alternativas[:MAX_AMBIGUOUS],
                    action=acao, rejected_product_id=rejeitado,
                )
        return CommerceTurnOutcome(outcome=OUTCOME_REJECTED, action=acao,
                                   rejected_product_id=rejeitado)

    # --- ambiguidade vinda da lista apresentada -----------------------------
    if resolucao.requires_clarification and resolucao.candidates:
        candidatos = [
            {"id": c.product_id, "name": c.name, "reference": c.reference}
            for c in resolucao.candidates[:MAX_AMBIGUOUS]
        ]
        # As opcoes viram a nova lista apresentada: logo apos "Qual destas voce
        # procura? 1... 2...", o cliente responde "o segundo" — e a posicao
        # precisa contar sobre o que ele acabou de ver.
        _guardar_lista(state, candidatos)
        state.last_commerce_action = acao
        return CommerceTurnOutcome(
            outcome=OUTCOME_AMBIGUOUS, products=candidatos, action=acao
        )

    # --- qual produto? ------------------------------------------------------
    produto: dict[str, Any] | None = None
    if resolucao.resolved_product is not None:
        referencia = resolucao.resolved_product
        detalhado = await _detalhar(execute, referencia.product_id)
        produto = detalhado or {
            "id": referencia.product_id,
            "name": referencia.name,
            "reference": referencia.reference,
        }
    elif resolucao.requires_catalog_search:
        # O codigo do produto vem primeiro quando existe. Numa fonte onde a
        # coluna de referencia costuma vir vazia, ele aparece DENTRO do nome —
        # entao a busca por codigo tambem e lexical, e e a mais especifica que
        # existe: "AD-190" acha um produto, a descricao inteira acha nenhum.
        codigo = resolucao.explicit_reference or resolucao.explicit_ean
        consulta = codigo or resolucao.product_query
        if not consulta:
            return CommerceTurnOutcome(outcome=OUTCOME_CLARIFICATION, action=acao)
        if codigo:
            encontrados, ok = await _buscar_literal(execute, codigo)
        else:
            encontrados, ok = await _buscar(execute, consulta)
        if not ok:
            return CommerceTurnOutcome(outcome=OUTCOME_PROVIDER_UNAVAILABLE, action=acao)
        if not encontrados and codigo and resolucao.product_query:
            encontrados, ok = await _buscar(execute, resolucao.product_query)
            if not ok:
                return CommerceTurnOutcome(
                    outcome=OUTCOME_PROVIDER_UNAVAILABLE, action=acao
                )
            consulta = resolucao.product_query
        decisao = match_generic_catalog_product(consulta, encontrados)
        if decisao.ambiguous:
            _guardar_lista(state, decisao.candidates)
            state.last_commerce_action = acao
            return CommerceTurnOutcome(
                outcome=OUTCOME_AMBIGUOUS, products=decisao.candidates, action=acao
            )
        if decisao.matched is None:
            state.last_commerce_action = acao
            return CommerceTurnOutcome(outcome=OUTCOME_PRODUCT_NOT_FOUND, action=acao)
        produto = dict(decisao.matched)
        detalhado = await _detalhar(execute, produto.get("id") or produto.get("external_id"))
        if detalhado:
            produto.update(detalhado)

    if produto is None:
        state.last_commerce_action = acao
        return CommerceTurnOutcome(outcome=OUTCOME_CLARIFICATION, action=acao)

    _ativar(state, produto)
    if acao == ACTION_CORRECT_REFERENCE:
        state.last_commerce_action = acao
        return CommerceTurnOutcome(outcome=OUTCOME_PRODUCT, product=produto, action=acao)

    # --- o fato pedido ------------------------------------------------------
    identificador = str(produto.get("id") or produto.get("external_id") or "")

    if acao in {ACTION_SHOW_MEDIA, ACTION_SHOW_MORE_MEDIA}:
        imagens = product_media_urls(produto)
        state.last_commerce_action = acao
        state.last_requested_fact = FACT_MEDIA
        if not imagens:
            state.last_media_product_id = identificador
            state.last_media_index = 0
            return CommerceTurnOutcome(
                outcome=OUTCOME_MEDIA_UNAVAILABLE, product=produto, action=acao
            )
        if acao == ACTION_SHOW_MORE_MEDIA and state.last_media_product_id == identificador:
            proximo = state.last_media_index + 1
        else:
            proximo = 0
        if proximo >= len(imagens):
            return CommerceTurnOutcome(
                outcome=OUTCOME_MEDIA_UNAVAILABLE, product=produto, action=acao,
                only_one_media=len(imagens) == 1,
            )
        state.last_media_product_id = identificador
        state.last_media_index = proximo
        return CommerceTurnOutcome(
            outcome=OUTCOME_MEDIA, product=produto, media=[imagens[proximo]], action=acao
        )

    if acao == ACTION_CHECK_INVENTORY:
        estoque = await execute("check_inventory", {"product_id": identificador})
        state.last_commerce_action = acao
        state.last_requested_fact = resolucao.requested_fact
        if not estoque.get("ok"):
            return CommerceTurnOutcome(
                outcome=OUTCOME_PROVIDER_UNAVAILABLE, product=produto, action=acao
            )
        return CommerceTurnOutcome(
            outcome=OUTCOME_INVENTORY, product=produto, inventory=estoque, action=acao
        )

    state.last_commerce_action = acao
    state.last_requested_fact = resolucao.requested_fact
    if acao == ACTION_GET_PRICE:
        return CommerceTurnOutcome(outcome=OUTCOME_PRICE, product=produto, action=acao)
    if acao == ACTION_GET_DETAILS:
        return CommerceTurnOutcome(outcome=OUTCOME_DETAILS, product=produto, action=acao)
    if acao == ACTION_COMPARE:
        return CommerceTurnOutcome(outcome=OUTCOME_COMPARE, product=produto, action=acao)
    return CommerceTurnOutcome(outcome=OUTCOME_PRODUCT, product=produto, action=acao)


# === carrinho local ========================================================


def _invalidar_revisao(state) -> None:
    """Mexeu no carrinho, a revisao anterior deixou de valer.

    Confirmar uma revisao que nao corresponde mais ao carrinho cria o pedido
    errado — e pedido criado nao tem desfazer.
    """
    state.order_confirmation_status = "not_ready"
    state.confirmed_order_review_version = None
    if getattr(state, "pending_commerce_action", None) == PENDING_CONFIRM_ORDER:
        state.pending_commerce_action = None
    from ..commerce.order_review import order_fingerprint

    state.order_review_version = order_fingerprint(state)


def _item_do_carrinho(state, product_id: str):
    return next(
        (i for i in (state.cart_items or []) if str(i.product_id) == str(product_id)),
        None,
    )


def _preco_texto(produto: dict[str, Any]) -> str | None:
    valor = produto.get("price")
    return None if valor is None else str(valor)


def _adicionar(state, produto: dict[str, Any], quantidade: int) -> None:
    from ..commerce_context import CommerceCartItem

    identificador = str(produto.get("id") or produto.get("external_id") or "")
    existente = _item_do_carrinho(state, identificador)
    if existente is not None:
        existente.quantity = max(1, quantidade)
        return
    state.cart_items = [
        *(state.cart_items or []),
        CommerceCartItem(
            product_id=identificador,
            name=produto.get("name"),
            quantity=max(1, quantidade),
            unit_price=_preco_texto(produto),
        ),
    ]


async def _produto_do_turno(resolucao, *, state, execute) -> dict[str, Any] | None:
    """Sobre qual produto a operacao de carrinho recai."""
    referencia = resolucao.resolved_product or getattr(state, "active_product", None)
    if referencia is None:
        return None
    detalhado = await _detalhar(execute, referencia.product_id) if execute else None
    if detalhado:
        return detalhado
    item = _item_do_carrinho(state, referencia.product_id)
    return {
        "id": referencia.product_id,
        "name": referencia.name or (item.name if item else None),
        "reference": getattr(referencia, "reference", None),
        "price": float(item.unit_price) if item and item.unit_price else None,
    }


async def _turno_de_carrinho(acao, resolucao, *, state, execute) -> CommerceTurnOutcome:
    """Operacoes de carrinho. Nenhuma delas chama a fonte comercial.

    O adaptador serializa requisicoes e dorme dois segundos depois de cada uma:
    somar um item nao pode custar isso. Os fatos necessarios (nome, preco) ja
    vieram quando o produto entrou em jogo.
    """
    state.last_commerce_action = acao

    if acao == ACTION_SHOW_CART:
        return CommerceTurnOutcome(
            outcome=OUTCOME_CART, products=_carrinho_para_saida(state), action=acao
        )

    if acao == ACTION_CLEAR_CART:
        state.cart_items = []
        _invalidar_revisao(state)
        return CommerceTurnOutcome(outcome=OUTCOME_CART, products=[], action=acao)

    if acao == ACTION_CONFIRM_ORDER:
        return await _confirmar_pedido(state, acao, execute=execute)

    referencia = resolucao.resolved_product or getattr(state, "active_product", None)
    if referencia is None:
        return CommerceTurnOutcome(outcome=OUTCOME_CLARIFICATION, action=acao)

    if acao == ACTION_REMOVE_FROM_CART:
        state.cart_items = [
            i for i in (state.cart_items or [])
            if str(i.product_id) != str(referencia.product_id)
        ]
        _invalidar_revisao(state)
        return CommerceTurnOutcome(
            outcome=OUTCOME_CART, products=_carrinho_para_saida(state), action=acao
        )

    produto = {
        "id": referencia.product_id,
        "name": referencia.name,
        "reference": getattr(referencia, "reference", None),
        "price": None,
    }
    do_carrinho = _item_do_carrinho(state, referencia.product_id)
    if do_carrinho is not None and do_carrinho.unit_price:
        produto["price"] = float(do_carrinho.unit_price)

    if acao == ACTION_SET_QUANTITY:
        quantidade, incremental = resolucao.cart_quantity or (1, False)
        atual = do_carrinho.quantity if do_carrinho else 0
        nova = atual + quantidade if incremental else quantidade
        if nova < 1:
            # Quantidade zero nao e "remover por engano": mantem o item e pede
            # numero valido, porque apagar por causa de um numero errado perde
            # a escolha que o cliente ja fez.
            return CommerceTurnOutcome(outcome=OUTCOME_CLARIFICATION, action=acao)
        _adicionar(state, produto, nova)
        _invalidar_revisao(state)
        return CommerceTurnOutcome(
            outcome=OUTCOME_CART, products=_carrinho_para_saida(state), action=acao
        )

    quantidade = 1
    if resolucao.cart_quantity:
        quantidade = max(1, resolucao.cart_quantity[0])
    _adicionar(state, produto, (do_carrinho.quantity if do_carrinho else 0) + quantidade
               if do_carrinho else quantidade)
    _invalidar_revisao(state)
    return CommerceTurnOutcome(
        outcome=OUTCOME_CART, products=_carrinho_para_saida(state), action=acao
    )


def _carrinho_para_saida(state) -> list[dict[str, Any]]:
    return [
        {
            "id": i.product_id,
            "name": i.name,
            "quantity": i.quantity,
            "price": float(i.unit_price) if i.unit_price else None,
        }
        for i in (state.cart_items or [])
    ]


async def _confirmar_pedido(state, acao, *, execute) -> CommerceTurnOutcome:
    """Confirma a revisao que ja existe. Nao consulta nada.

    Toda a resolucao — produto, preco, estoque, condicao de pagamento —
    aconteceu na revisao. Refazer aqui seria pior que redundante: o cliente
    aprovou o que viu, e um preco ou prazo buscado DEPOIS do "confirmo" poderia
    nao ser o mesmo que ele aprovou.

    Por isso a confirmacao so verifica que existe revisao pendente e carimba
    exatamente aquela versao.
    """
    pendente = getattr(state, "pending_commerce_action", None) == PENDING_CONFIRM_ORDER
    if not pendente or state.order_confirmation_status != "pending":
        # "confirmo" sem revisao pendente nao autoriza nada: seria o cliente
        # aprovando um pedido que ele nunca viu.
        state.order_confirmation_status = "not_ready"
        return CommerceTurnOutcome(outcome=OUTCOME_CLARIFICATION, action=acao)

    versao = getattr(state, "order_review_version", None)
    if not versao:
        state.order_confirmation_status = "not_ready"
        return CommerceTurnOutcome(
            outcome=OUTCOME_ORDER_INCOMPLETE, action=acao,
            products=_carrinho_para_saida(state),
        )

    state.pending_commerce_action = None
    state.order_confirmation_status = "confirmed"
    state.confirmed_order_review_version = versao

    # Portao fechado nesta fase: o pedido fica PRONTO, nunca criado.
    return CommerceTurnOutcome(
        outcome=OUTCOME_ORDER_READY, action=acao,
        products=_carrinho_para_saida(state),
    )


async def _turno_de_revisao(*, state, execute, fetch_conditions) -> CommerceTurnOutcome:
    """Pedido explicito de fechar/revisar. Delega a regra, nao a reimplementa.

    Toda a validacao — preco, estoque, condicao de pagamento, digital — vive em
    `run_order_review`. Aqui so se escolhe QUANDO ela roda e como o resultado
    volta para a conversa.
    """
    from ..commerce.order_review import REVIEW_READY, run_order_review

    state.last_commerce_action = ACTION_REVIEW_ORDER

    if not (state.cart_items or []):
        return CommerceTurnOutcome(
            outcome=OUTCOME_CART, products=[], action=ACTION_REVIEW_ORDER
        )

    if fetch_conditions is None:
        from ..commerce.tools import get_commerce_provider

        async def fetch_conditions():  # noqa: F811 - fonte padrao do runtime
            # Capacidade PUBLICA do provider. Alcancar o cliente por dentro
            # funcionava, mas amarrava o fluxo a fiacao de um fornecedor — e,
            # feito por `getattr` com string, escapava de qualquer busca.
            provider = get_commerce_provider()
            leitor = getattr(provider, "list_payment_conditions", None)
            if leitor is None:
                return []
            return await leitor()

    revisao = await run_order_review(
        state, execute=execute, fetch_conditions=fetch_conditions
    )
    pronta = revisao.status == REVIEW_READY
    if pronta:
        # Revisao aprovavel: a proxima palavra do cliente pode ser o "sim".
        state.pending_commerce_action = PENDING_CONFIRM_ORDER

    return CommerceTurnOutcome(
        outcome=OUTCOME_ORDER_REVIEW if pronta else OUTCOME_ORDER_INCOMPLETE,
        action=ACTION_REVIEW_ORDER,
        products=_carrinho_para_saida(state),
        missing=list(revisao.missing),
        unconfirmed=list(revisao.unconfirmed),
        review=revisao,
    )
