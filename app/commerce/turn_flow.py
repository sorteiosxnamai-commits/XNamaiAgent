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


async def run_commerce_turn(text: str, *, state, execute) -> CommerceTurnOutcome:
    """Um turno comercial completo, do texto ao fato — atualizando o estado."""
    resolucao = resolve_commerce_turn(text, state=state)
    acao = resolucao.action

    # --- browse -------------------------------------------------------------
    if acao == ACTION_BROWSE_CATALOG:
        quantos = BROWSE_PAGE_SIZE
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
        consulta = resolucao.product_query or resolucao.explicit_reference or resolucao.explicit_ean
        if not consulta:
            return CommerceTurnOutcome(outcome=OUTCOME_CLARIFICATION, action=acao)
        encontrados, ok = await _buscar(execute, consulta)
        if not ok:
            return CommerceTurnOutcome(outcome=OUTCOME_PROVIDER_UNAVAILABLE, action=acao)
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
