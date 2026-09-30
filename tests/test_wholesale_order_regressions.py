from unittest.mock import AsyncMock

import pytest

from app.agent_contracts import build_agent_decision
from app.commerce.catalog_filters import matches
from app.commerce.mercos.order_contents import normalize_contents
from app.commerce.mercos.order_status import normalize_status, status_result
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.context_resume import build_contextual_greeting
from app.factual_validator import validate_factual_response
from app.greeting_policy import choose_greeting_reply
from app.models import AgentResult, IncomingMessage
from app.order_queries import handle_order_query, resolve_order_query
from app.response_composer import compose_outbound_reply
from app.wholesale_catalog import handle_wholesale_catalog, requested_queries


def order_data(count=3):
    return {"id": 166937881, "numero": 95694, "status": 0, "total": count * 20,
            "cliente_cnpj": "private", "cliente_email": "private",
            "itens": [{"produto_id": n + 1, "produto_nome": f"Produto {n + 1}",
                       "quantidade": 2, "preco_liquido": 10, "subtotal": 20,
                       "observacoes": "private"} for n in range(count)]}


def report(result):
    decision = build_agent_decision(IncomingMessage(channel="whatsapp", text="itens do pedido"), result, openai_call_count=0)
    return validate_factual_response(result, decision=decision, mode="enforce")


def test_order_contents_allowlist_does_not_copy_private_fields_or_infer_missing():
    normalized = normalize_contents(order_data())
    assert normalized["item_count"] == 3
    assert "private" not in str(normalized)
    assert normalize_contents({"total": 30}) is None
    assert normalize_contents({"itens": [{"produto_id": 1}]}) is None
    assert normalize_contents({"itens": []})["items_confirmed"] is True
    assert normalize_contents({"itens": [{"excluido": True}]})["items"] == []


@pytest.mark.asyncio
async def test_original_order_sequence_never_searches_catalog_or_invents_count():
    state = CommerceConversationState(order_id="166937881", active_topic="order_status",
        last_presented_products=[{"product_id": "wrong", "name": "Película antiga", "position": 1}])
    facts = status_result([normalize_status(order_data())], "95694")
    execute = AsyncMock(return_value=facts)
    for message in ("quais os itens desse pedido?", "só tem um item?", "não precisa, consulta outro para mim, 95694", "consegue listar o pedido completo?"):
        result = await handle_order_query(message, state=state, execute=execute)
        assert result is not None and "Película antiga" not in result.reply_text
        if message != "não precisa, consulta outro para mim, 95694":
            assert "3 itens" in result.reply_text and "Produto 3" in result.reply_text
            assert report(result).valid
        state = evolve_commerce_state(state, result)
    assert {call.args[0] for call in execute.await_args_list} == {"get_order_complete"}
    assert execute.await_args_list[2].args[1]["order_id"] == "95694"


@pytest.mark.asyncio
async def test_unavailable_contents_are_not_replaced_with_a_catalog_product():
    execute = AsyncMock(return_value=status_result([normalize_status({"id": 11, "numero": 95694, "status": 0})], "95694"))
    result = await handle_order_query("itens do pedido 95694", state=CommerceConversationState(), execute=execute)
    assert "itens completos" in result.reply_text
    assert "não consigo confirmar" in result.reply_text.lower()
    assert result.commercial_data["items_confirmed"] is False
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_long_order_continuation_preserves_every_item_and_recorded_prices():
    execute = AsyncMock(return_value=status_result([normalize_status(order_data(140))], "95694"))
    state = CommerceConversationState(order_id="166937881", active_topic="order_status")
    seen = []
    text = "listar o pedido completo"
    for _ in range(30):
        result = await handle_order_query(text, state=state, execute=execute)
        result = compose_outbound_reply(IncomingMessage(channel="whatsapp", text=text), result, max_reply_chars=900)
        assert len(result.reply_text) < 4096 and "…" not in result.reply_text
        import re
        seen.extend(int(n) for n in re.findall(r"^\d+\. Produto (\d+)", result.reply_text, re.M))
        state = evolve_commerce_state(state, result)
        if state.order_items_offset == 140:
            break
        text = "continue"
    assert seen == list(range(1, 141))


def test_order_item_count_and_catalog_mix_fail_the_factual_gate():
    result = AgentResult(reply_text="No pedido 95694 apareceu só 1 item.", intent="commerce",
                         response_metadata={"domain": "commerce", "goal": "after_sales"})
    assert not report(result).valid
    result.commercial_data = {"products": [{"id": "1", "name": "Fone", "price": 10}]}
    result.reply_text = "Encontrei este fone."
    assert not report(result).valid


@pytest.mark.parametrize("name,expected", [
    ("Caixa de Som Bluetooth 120 W Kimaster", True),
    ("Caixa de Som 120W RMS", True), ("Caixa de Som 1200W", False),
    ("Caixa de Som 80W", False), ("Aspirador 120W", False),
])
def test_power_and_category_are_both_mandatory(name, expected):
    assert matches("caixa de som 120w", {"name": name}) is expected


@pytest.mark.asyncio
async def test_multi_category_and_unlimited_continuation_survive_composer_and_state():
    products = [{"id": str(n), "name": f"Fone de Ouvido Modelo {n}", "price": 10} for n in range(130)]
    products += [{"id": "mouse", "name": "Mouse Gamer", "price": 20}]
    calls = []
    async def execute(tool, args):
        assert tool == "search_products" and args["strict"]
        calls.append(args)
        selected = [p for p in products if matches(args["query"], p)]
        offset, size = args["offset"], args["limit"]
        return {"ok": True, "products": selected[offset:offset + size],
                "paging": {"has_more": offset + size < len(selected)}}
    state = CommerceConversationState()
    text = "gostaria de saber sobre os seus produtos, fone de ouvido , mouse."
    seen = []
    for _ in range(40):
        result = await handle_wholesale_catalog(text, state=state, execute=execute)
        result = compose_outbound_reply(IncomingMessage(channel="whatsapp", text=text), result, max_reply_chars=900)
        assert len(result.reply_text) < 4096 and not result.reply_text.endswith("…")
        shown = result.commercial_data["products"]
        seen.extend(p["id"] for p in shown)
        for product in shown:
            assert product["name"] in result.reply_text
        state = evolve_commerce_state(state, result)
        state = CommerceConversationState.from_payload(state.model_dump(mode="json"))
        assert len(state.last_presented_products) == len(shown)
        if all(q["done"] for q in state.catalog_listing["queries"]):
            break
        text = "continue"
    assert len(seen) == 131 and len(set(seen)) == 131
    assert calls[0]["query"] == "fone de ouvido" and calls[1]["query"] == "mouse"
    assert max(call["offset"] for call in calls) > 100


def test_refinement_retains_category_and_replaces_old_power():
    state = CommerceConversationState(catalog_listing={"queries": [{"query": "caixa de som 80w", "offset": 40}]})
    query = requested_queries("só 120w", state)[0]
    assert query["query"] == "caixa de som 120w" and query["offset"] == 0


def test_accessory_and_brand_constraints_are_not_separate_categories():
    queries = requested_queries("caixa de som 120w com microfone, fone de ouvido da marca JBL", CommerceConversationState())
    assert len(queries) == 2
    assert matches(queries[0]["query"], {"name": "Caixa de Som 120W com Microfone"})
    assert not matches(queries[0]["query"], {"name": "Caixa de Som 120W"})
    assert matches(queries[1]["query"], {"name": "Fone Bluetooth JBL"})
    assert not matches(queries[1]["query"], {"name": "Fone Bluetooth OutraMarca"})
    assert matches("caixa de som 2,5w", {"name": "Caixa de Som 2.5 W"})


@pytest.mark.parametrize("text", ["comprei um mouse com defeito", "quero comprar 2 fones", "quero 10 caixas de som", "quero trocar o mouse", "adicionar fones no carrinho"])
def test_wholesale_listing_does_not_steal_purchase_or_support(text):
    assert requested_queries(text, CommerceConversationState()) is None


@pytest.mark.parametrize("text", ["pedido visual do produto ativo", "adiciona itens no carrinho", "quero fazer um pedido", "qual o preço do mouse?", "consulta um mouse", "sim"])
def test_new_order_route_does_not_steal_product_or_cart_requests(text):
    assert resolve_order_query(text, CommerceConversationState(order_id="11", active_topic="order_status")) is None


def test_mouse_is_not_a_mousepad_or_toy_rat():
    assert not matches("mouse", {"name": "Mouse Pad com gel"})
    assert not matches("mouse", {"name": "Rato para gato"})
    assert matches("mouse", {"name": "Mouse gamer wireless"})


def test_wholesale_repository_uses_real_tenant_guard_and_keeps_database_errors(monkeypatch):
    from contextlib import contextmanager
    from app import db
    from app.catalog_index_repository import CatalogIndexRepository
    class Connection:
        broken = False
        @contextmanager
        def cursor(self):
            yield self
        def execute(self, sql, params):
            assert params["tenant_id"] == "xnamai"
            assert "tenant_id=%(tenant_id)s" in sql
            assert params["offset"] == 140
            if self.broken:
                raise RuntimeError("database temporarily unavailable")
        def fetchall(self):
            return [{"product_id": "confirmed"}]
    conn = Connection()
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(db, "get_conn", connection)
    repo = CatalogIndexRepository()
    assert repo.search_wholesale(tenant_id="xnamai", query="mouse", offset=140) == [{"product_id": "confirmed"}]
    conn.broken = True
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        repo.search_wholesale(tenant_id="xnamai", query="mouse", offset=140)


@pytest.mark.asyncio
async def test_inbound_pipeline_keeps_order_contents_and_category_routes_separate(monkeypatch):
    from tests.test_chatbo_pipeline_replay import Replay
    from app import openai_agent, sales_agent
    from app.config import get_settings
    replay = Replay(monkeypatch)
    monkeypatch.setenv("MERCOS_ADAPTOR_URL", "https://adapter.example")
    monkeypatch.setenv("MERCOS_ADAPTOR_API_KEY", "test-placeholder")
    get_settings.cache_clear()
    products = [{"id": "headphone", "name": "Fone de ouvido", "price": 10},
                {"id": "mouse", "name": "Mouse gamer", "price": 20}]
    async def execute(tool, args):
        replay.calls.append((tool, args))
        if tool == "get_order_complete":
            return status_result([normalize_status(order_data())], str(args["order_id"]))
        assert tool == "search_products" and args["strict"] is True
        return {"ok": True, "products": [p for p in products if matches(args["query"], p)], "paging": {"has_more": False}}
    monkeypatch.setattr(openai_agent, "execute_tool", execute)
    monkeypatch.setattr(sales_agent, "execute_tool", execute)
    await replay.say("fiz um pedido no carrinho")
    await replay.say("95694")
    result, _ = await replay.say("quais os itens desse pedido?")
    assert "3 itens" in result.reply_text and "Produto 3" in result.reply_text
    assert result.response_metadata["factual_validation"]["valid"]
    result, _ = await replay.say("gostaria de saber sobre seus produtos, fone de ouvido, mouse")
    assert "Fone de ouvido" in result.reply_text and "Mouse gamer" in result.reply_text
    assert result.response_metadata["factual_validation"]["valid"]


@pytest.mark.asyncio
async def test_provider_error_is_not_no_matching_products():
    result = await handle_wholesale_catalog("mouse", state=CommerceConversationState(),
        execute=AsyncMock(return_value={"ok": False, "error": "timeout"}))
    assert "não consegui consultar" in result.reply_text and "Não encontrei" not in result.reply_text


def test_repeated_and_contextual_greetings_always_retain_published_identity():
    identity = {"agent_name": "Mai", "brand": "XNamai"}
    history = []
    for _ in range(12):
        text = choose_greeting_reply(history, identity)
        assert "Mai" in text and "XNamai" in text and "😊" in text
        if history:
            assert text != history[-1]["content"]
        history.append({"role": "assistant", "content": text})
    result = build_contextual_greeting(CommerceConversationState(order_id="95694"), persona_identity=identity, recent_turns=history)
    assert "Mai" in result.reply_text and "😊" in result.reply_text and "95694" not in result.reply_text
