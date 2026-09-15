"""As conversas inteiras: lista -> selecao -> preco -> estoque -> foto.

Aqui o resolver (puro) encontra a fonte de fatos (tools) e o estado que segura o
produto entre turnos. Nenhum teste toca rede: o executor de tool e injetado.

O que estes testes protegem, acima de tudo, e a CONTINUIDADE. Cada turno curto
("quanto custa?", "tem foto?", "manda outra") depende do turno anterior, e era
exatamente ai que a conversa se perdia — cada mensagem virava busca nova e o
agente respondia "nao encontrei esse produto" sobre o produto que ele mesmo
acabara de mostrar.

E protegem uma distincao que o cliente sente: produto inexistente, produto sem
foto e catalogo fora do ar sao tres coisas diferentes. Responder "nao encontrei
esse produto" quando o produto existe mas nao tem imagem faz o cliente desistir
de algo que estava disponivel.
"""

from __future__ import annotations

import pytest

from app.commerce.turn_flow import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_BROWSE,
    OUTCOME_INVENTORY,
    OUTCOME_MEDIA,
    OUTCOME_MEDIA_UNAVAILABLE,
    OUTCOME_PRICE,
    OUTCOME_PRODUCT,
    OUTCOME_PRODUCT_NOT_FOUND,
    OUTCOME_PROVIDER_UNAVAILABLE,
    run_commerce_turn,
)
from app.commerce_context import CommerceConversationState

RING = {"id": "1", "name": "Q508A - Ring Light Bastao Led RGB", "price": 180.0, "stock": 11}
TIPOC = {"id": "2", "name": "FKT-110C - Tipo C Kit Carregador USB", "price": 14.91, "stock": 490}
V8 = {"id": "3", "name": "FKT-1108 - V8 Kit Carregador USB", "price": 14.04, "stock": 101}
CABO = {"id": "4", "name": "CB702V - V8 Cabo Usb 1 Metro", "price": 5.25, "stock": 69}
LISTA = [RING, TIPOC, V8, CABO]


def _executor(*, produtos=LISTA, busca=None, falha=False):
    chamadas: list[tuple[str, dict]] = []

    async def executar(tool, args):
        chamadas.append((tool, dict(args)))
        if falha:
            return {"ok": False, "error": "commerce_provider_unavailable"}
        if tool == "search_products":
            achados = busca(args) if callable(busca) else produtos
            return {"ok": True, "products": list(achados)}
        alvo = next((p for p in produtos if p["id"] == str(args.get("product_id"))), None)
        if alvo is None:
            return {"ok": False, "error": "product_not_found"}
        if tool == "get_product":
            return {"ok": True, "product": alvo}
        return {"ok": True, "stock": alvo.get("stock"), "stock_confirmed": True}

    executar.chamadas = chamadas
    return executar


def _ids(executar, tool):
    return [str(a.get("product_id")) for t, a in executar.chamadas if t == tool]


# === CONVERSA 1: lista -> selecao por palavras -> fatos ====================


@pytest.mark.asyncio
async def test_conversation_list_select_price_inventory_media():
    estado = CommerceConversationState()
    executar = _executor()

    # TURN 1 — a lista fica guardada, e nenhum produto vira ativo sozinho.
    r1 = await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    assert r1.outcome == OUTCOME_BROWSE
    assert [p.product_id for p in estado.last_presented_products] == ["1", "2", "3", "4"]
    assert estado.active_product is None, "produto 1 nao pode ser ativado por conta propria"

    # TURN 2 — "o carregador tipo c" resolve na lista, sem varrer o catalogo.
    antes = len(executar.chamadas)
    r2 = await run_commerce_turn("quero o carregador tipo c", state=estado, execute=executar)
    assert r2.outcome == OUTCOME_PRODUCT
    assert estado.active_product.product_id == "2"
    buscas = [t for t, _ in executar.chamadas[antes:] if t == "search_products"]
    assert buscas == [], "foi ao catalogo global com a lista ja resolvendo"

    # TURN 3 — preco do produto selecionado.
    r3 = await run_commerce_turn("quanto custa?", state=estado, execute=executar)
    assert r3.outcome == OUTCOME_PRICE
    assert _ids(executar, "get_product")[-1] == "2"

    # TURN 4 — estoque do mesmo produto.
    r4 = await run_commerce_turn("tem estoque?", state=estado, execute=executar)
    assert r4.outcome == OUTCOME_INVENTORY
    assert _ids(executar, "check_inventory")[-1] == "2"

    # TURN 5 — foto: sem imagem no catalogo, mas o produto NAO some.
    r5 = await run_commerce_turn("tem foto?", state=estado, execute=executar)
    assert r5.outcome == OUTCOME_MEDIA_UNAVAILABLE
    assert r5.outcome != OUTCOME_PRODUCT_NOT_FOUND
    assert estado.active_product.product_id == "2"


# === CONVERSA 2: posicao na lista ==========================================


@pytest.mark.asyncio
async def test_conversation_by_position():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)

    await run_commerce_turn("o segundo", state=estado, execute=executar)
    assert estado.active_product.product_id == "2"

    r = await run_commerce_turn("esse tem estoque?", state=estado, execute=executar)
    assert r.outcome == OUTCOME_INVENTORY
    assert _ids(executar, "check_inventory")[-1] == "2"

    r = await run_commerce_turn("quanto ele custa?", state=estado, execute=executar)
    assert r.outcome == OUTCOME_PRICE
    assert _ids(executar, "get_product")[-1] == "2"


# === CONVERSA 3: ambiguidade nao vira escolha ==============================


@pytest.mark.asyncio
async def test_conversation_ambiguous_then_narrowed():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)

    r = await run_commerce_turn("quero o carregador", state=estado, execute=executar)
    assert r.outcome == OUTCOME_AMBIGUOUS
    assert {p["id"] for p in r.products} == {"2", "3"}
    assert estado.active_product is None, "escolheu um dos dois por conta propria"

    r = await run_commerce_turn("o tipo c", state=estado, execute=executar)
    assert estado.active_product.product_id == "2"


# === CONVERSA 4-5: busca global e continuidade =============================


@pytest.mark.asyncio
async def test_a_new_product_name_searches_the_catalog():
    estado = CommerceConversationState()
    executar = _executor(produtos=[CABO], busca=lambda a: [CABO])
    r = await run_commerce_turn("tem cabo usb?", state=estado, execute=executar)
    assert r.outcome == OUTCOME_PRODUCT
    assert estado.active_product.product_id == "4"


@pytest.mark.asyncio
async def test_follow_ups_keep_the_same_product_id():
    estado = CommerceConversationState()
    executar = _executor(produtos=[CABO], busca=lambda a: [CABO])
    await run_commerce_turn("quanto custa o CB702V - V8 Cabo Usb 1 Metro?", state=estado, execute=executar)
    await run_commerce_turn("e o estoque?", state=estado, execute=executar)
    await run_commerce_turn("tem foto?", state=estado, execute=executar)

    assert set(_ids(executar, "check_inventory")) == {"4"}
    assert set(_ids(executar, "get_product")) <= {"4"}
    assert estado.active_product.product_id == "4"


# === CONVERSA 6: correcao ==================================================


@pytest.mark.asyncio
async def test_a_correction_moves_the_active_product():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    await run_commerce_turn("quero o segundo", state=estado, execute=executar)
    assert estado.active_product.product_id == "2"

    await run_commerce_turn("nao, o terceiro", state=estado, execute=executar)
    assert estado.active_product.product_id == "3"
    assert estado.previous_active_product.product_id == "2"


# === CONVERSA 7: troca de assunto ==========================================


@pytest.mark.asyncio
async def test_naming_another_product_replaces_the_active_one():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    await run_commerce_turn("quero o carregador tipo c", state=estado, execute=executar)
    assert estado.active_product.product_id == "2"

    await run_commerce_turn("e tem ring light?", state=estado, execute=executar)
    assert estado.active_product.product_id == "1"


# === CONVERSA 8: rejeicao ==================================================


@pytest.mark.asyncio
async def test_rejecting_the_active_product_clears_it():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    await run_commerce_turn("o segundo", state=estado, execute=executar)

    r = await run_commerce_turn("nao quero esse, tem outro parecido?", state=estado, execute=executar)
    assert estado.active_product is None or estado.active_product.product_id != "2"
    assert r.rejected_product_id == "2"


# === midia: tres estados distintos =========================================


@pytest.mark.asyncio
async def test_media_unavailable_is_not_product_not_found():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    await run_commerce_turn("o primeiro", state=estado, execute=executar)

    r = await run_commerce_turn("manda foto", state=estado, execute=executar)
    assert r.outcome == OUTCOME_MEDIA_UNAVAILABLE
    assert r.product is not None, "o produto continua conhecido"


@pytest.mark.asyncio
async def test_media_is_served_when_the_catalog_has_an_image():
    com_foto = {**RING, "image_url": "https://exemplo.invalid/a.jpg"}
    estado = CommerceConversationState()
    executar = _executor(produtos=[com_foto], busca=lambda a: [com_foto])
    await run_commerce_turn("tem ring light?", state=estado, execute=executar)

    r = await run_commerce_turn("tem foto?", state=estado, execute=executar)
    assert r.outcome == OUTCOME_MEDIA
    assert r.media == ["https://exemplo.invalid/a.jpg"]


@pytest.mark.asyncio
async def test_another_image_walks_the_media_list():
    com_fotos = {**RING, "images": ["https://exemplo.invalid/a.jpg", "https://exemplo.invalid/b.jpg"]}
    estado = CommerceConversationState()
    executar = _executor(produtos=[com_fotos], busca=lambda a: [com_fotos])
    await run_commerce_turn("tem ring light?", state=estado, execute=executar)

    primeira = await run_commerce_turn("tem foto?", state=estado, execute=executar)
    segunda = await run_commerce_turn("manda outra", state=estado, execute=executar)
    assert primeira.media == ["https://exemplo.invalid/a.jpg"]
    assert segunda.media == ["https://exemplo.invalid/b.jpg"]
    assert estado.last_media_index == 1


@pytest.mark.asyncio
async def test_a_single_image_says_it_is_the_only_one():
    com_foto = {**RING, "image_url": "https://exemplo.invalid/a.jpg"}
    estado = CommerceConversationState()
    executar = _executor(produtos=[com_foto], busca=lambda a: [com_foto])
    await run_commerce_turn("tem ring light?", state=estado, execute=executar)
    await run_commerce_turn("tem foto?", state=estado, execute=executar)

    r = await run_commerce_turn("manda outra", state=estado, execute=executar)
    assert r.outcome == OUTCOME_MEDIA_UNAVAILABLE
    assert r.only_one_media is True


@pytest.mark.asyncio
async def test_a_failing_provider_is_not_a_missing_product():
    """Tres estados que nao podem se confundir."""
    estado = CommerceConversationState()
    executar = _executor(falha=True)
    r = await run_commerce_turn("tem cabo lightning?", state=estado, execute=executar)
    assert r.outcome == OUTCOME_PROVIDER_UNAVAILABLE
    assert r.outcome != OUTCOME_PRODUCT_NOT_FOUND


@pytest.mark.asyncio
async def test_a_genuinely_absent_product_is_not_found():
    estado = CommerceConversationState()
    executar = _executor(produtos=[], busca=lambda a: [])
    r = await run_commerce_turn("tem helicoptero de plutonio?", state=estado, execute=executar)
    assert r.outcome == OUTCOME_PRODUCT_NOT_FOUND


# === estado persistido =====================================================


@pytest.mark.asyncio
async def test_the_state_remembers_the_last_action_and_fact():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    await run_commerce_turn("o segundo", state=estado, execute=executar)
    await run_commerce_turn("tem estoque?", state=estado, execute=executar)

    assert estado.last_commerce_action == "check_inventory"
    assert estado.last_requested_fact == "inventory"


@pytest.mark.asyncio
async def test_the_active_product_keeps_identity_fields_not_reply_text():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    await run_commerce_turn("o segundo", state=estado, execute=executar)

    ativo = estado.active_product
    assert ativo.product_id == "2"
    assert ativo.name == TIPOC["name"]


@pytest.mark.asyncio
async def test_browse_does_not_activate_any_product():
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("o que voces vendem?", state=estado, execute=executar)
    assert estado.active_product is None
    assert estado.last_commerce_action == "browse_catalog"


@pytest.mark.asyncio
async def test_after_a_disambiguation_the_options_become_the_list():
    """"Qual destas?" seguido de "o segundo" conta sobre as OPCOES mostradas."""
    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)

    r = await run_commerce_turn("quero o carregador", state=estado, execute=executar)
    assert r.outcome == OUTCOME_AMBIGUOUS
    assert [p.product_id for p in estado.last_presented_products] == ["2", "3"]

    await run_commerce_turn("o segundo", state=estado, execute=executar)
    assert estado.active_product.product_id == "3"


# === o estado precisa atravessar o turno (ida e volta pelo metadata) =======


def _proximo_turno(resultado_anterior, state_anterior):
    """Reconstroi o estado como o runtime faz entre mensagens."""
    from app.commerce_context import CommerceConversationState, evolve_commerce_state

    return evolve_commerce_state(CommerceConversationState(), resultado_anterior)


@pytest.mark.asyncio
async def test_the_rendered_turn_carries_the_state_forward():
    """Sem isto, cada mensagem recomeca do zero — foi o que producao mostrou.

    O estado do proximo turno e reconstruido do `response_metadata` do turno
    anterior. Um fast path que resolve tudo em memoria e nao publica nada no
    metadata perde o produto ativo assim que a mensagem termina.
    """
    from app.sales_agent import _render_commerce_turn

    estado = CommerceConversationState()
    executar = _executor()

    browse = await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    render_browse = _render_commerce_turn(browse, estado)
    assert render_browse.response_metadata.get("presented_products") is True
    estado2 = _proximo_turno(render_browse, estado)
    assert [p.product_id for p in estado2.last_presented_products] == ["1", "2", "3", "4"]

    escolha = await run_commerce_turn("o segundo", state=estado2, execute=executar)
    render_escolha = _render_commerce_turn(escolha, estado2)
    assert render_escolha.response_metadata.get("active_product", {}).get("product_id") == "2"

    estado3 = _proximo_turno(render_escolha, estado2)
    assert estado3.active_product is not None, "produto ativo nao sobreviveu ao turno"
    assert estado3.active_product.product_id == "2"

    preco = await run_commerce_turn("quanto custa?", state=estado3, execute=executar)
    assert preco.outcome == OUTCOME_PRICE
    assert _ids(executar, "get_product")[-1] == "2"


@pytest.mark.asyncio
async def test_the_last_action_survives_the_turn():
    from app.commerce_context import CommerceConversationState, evolve_commerce_state
    from app.sales_agent import _render_commerce_turn

    estado = CommerceConversationState()
    executar = _executor()
    await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    await run_commerce_turn("o primeiro", state=estado, execute=executar)
    midia = await run_commerce_turn("tem foto?", state=estado, execute=executar)

    render = _render_commerce_turn(midia, estado)
    proximo = evolve_commerce_state(CommerceConversationState(), render)
    assert proximo.last_commerce_action == "show_media"
    assert proximo.last_requested_fact == "media"


@pytest.mark.asyncio
async def test_a_presented_list_longer_than_three_keeps_its_positions():
    """"o quarto" so funciona se a lista inteira atravessar o turno."""
    from app.commerce_context import CommerceConversationState, evolve_commerce_state
    from app.sales_agent import _render_commerce_turn

    estado = CommerceConversationState()
    executar = _executor()
    browse = await run_commerce_turn("me mostre alguns produtos", state=estado, execute=executar)
    proximo = evolve_commerce_state(
        CommerceConversationState(), _render_commerce_turn(browse, estado)
    )
    assert len(proximo.last_presented_products) == 4
    assert proximo.last_presented_products[-1].position == 4
