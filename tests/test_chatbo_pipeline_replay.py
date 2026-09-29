"""Synthetic multi-request replay through the real inbound pipeline.

The recorder uses the existing response metadata and wraps existing handlers;
it does not add production observability or send customer data to a model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.models import IncomingMessage
from app.observability import redact_text


PRODUCTS = [
    {"id": "watch-1", "name": "Relógio Preto Esportivo", "price": 799, "stock": 5,
     "reference": "WATCH-1", "brand": "Marca Teste", "url": "https://example.com/watch-1"},
    {"id": "watch-2", "name": "Relógio Prata Clássico", "price": 999, "stock": 3,
     "reference": "WATCH-2", "brand": "Marca Teste", "url": "https://example.com/watch-2"},
]


class Replay:
    def __init__(self, monkeypatch, *, lookup=None, create=None):
        from app import message_pipeline, openai_agent, sales_agent, commerce_router
        from app.commerce import tools as commerce_tools
        from app.config import get_settings
        import app.account_flows as account_flows
        import app.customer_registration as registration

        self.states = {}
        self.history = {}
        self.rows = []
        self.calls = []
        self.handlers = []
        self.lookup = lookup or {"ok": True, "found": False}
        self.create = create or {"ok": True, "customer_id": "700"}
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("AGENT_FACTUAL_VALIDATION_MODE", "enforce")
        get_settings.cache_clear()

        def load(**kwargs):
            key = kwargs.get("conversation_id") or kwargs.get("sender_key") or kwargs.get("sender_phone")
            return json.loads(json.dumps(self.states.get(key, {})))

        def persist(*, commerce_state, conversation_id=None, sender_key=None, sender_phone=None, **_kwargs):
            key = conversation_id or sender_key or sender_phone
            self.states[key] = json.loads(json.dumps(commerce_state))

        monkeypatch.setattr(message_pipeline, "load_commerce_conversation_state", load)
        monkeypatch.setattr(message_pipeline, "persist_customer_commerce_session", persist)
        def load_history(**kwargs):
            key = kwargs.get("conversation_id") or kwargs.get("sender_key") or kwargs.get("sender_phone")
            return json.loads(json.dumps(self.history.get(key, [])))

        monkeypatch.setattr(message_pipeline, "load_recent_conversation_turns", load_history)
        monkeypatch.setattr(openai_agent, "load_recent_conversation_turns", load_history)
        monkeypatch.setattr("app.capability_catalog.runtime_commerce_capabilities",
                            lambda: frozenset({"create_customer", "lookup_customer_by_document",
                                               "search_products", "get_product", "check_inventory"}))
        class AvailableProvider:
            available = True
        monkeypatch.setattr("app.commerce.tools.get_commerce_provider", lambda: AvailableProvider())

        async def tool(name, args):
            self.calls.append((name, dict(args or {})))
            if name == "lookup_customer_by_document":
                return dict(self.lookup)
            if name == "create_customer":
                return dict(self.create)
            if name == "search_products":
                return {"ok": True, "products": list(PRODUCTS)}
            if name == "get_product":
                product_id = str(args.get("product_id"))
                product = next((p for p in PRODUCTS if p["id"] == product_id), PRODUCTS[0])
                return {"ok": True, "product": dict(product), **product}
            if name == "check_inventory":
                return {"ok": True, "stock": 5, "available": True}
            return {"ok": False, "error": "fixture_not_configured"}

        for module in (openai_agent, sales_agent, commerce_router, commerce_tools):
            monkeypatch.setattr(module, "execute_tool", tool)

        original_sales = sales_agent.handle_sales_message
        async def sales_spy(*args, **kwargs):
            result = await original_sales(*args, **kwargs)
            if result is not None:
                self.handlers.append("handle_sales_message")
            return result
        monkeypatch.setattr(openai_agent, "handle_sales_message", sales_spy)
        original_commerce = sales_agent.handle_commerce_message
        async def commerce_spy(*args, **kwargs):
            result = await original_commerce(*args, **kwargs)
            if result is not None:
                self.handlers.append("handle_commerce_message")
            return result
        monkeypatch.setattr(sales_agent, "handle_commerce_message", commerce_spy)

        original_account = account_flows.handle_account_flows
        async def account_spy(*args, **kwargs):
            result = await original_account(*args, **kwargs)
            if result is not None:
                self.handlers.append("handle_account_flows")
            return result
        monkeypatch.setattr(account_flows, "handle_account_flows", account_spy)

        original_registration = registration.handle_customer_registration_turn
        async def registration_spy(*args, **kwargs):
            result = await original_registration(*args, **kwargs)
            if result is not None:
                self.handlers.append("handle_customer_registration_turn")
            return result
        monkeypatch.setattr(registration, "handle_customer_registration_turn", registration_spy)
        monkeypatch.setattr(account_flows, "handle_customer_registration_turn", registration_spy)

    async def say(self, text, *, identity="a", via="conversation_id"):
        from app.message_pipeline import process_incoming_message
        suffix = {"a": 1, "b": 2}.get(identity, int(identity) if str(identity).isdigit() else 99)
        sender_phone = f"551198888{suffix:04d}"
        kwargs = {"conversation_id": f"wa:{sender_phone}"} if via == "conversation_id" else (
            {"sender_key": f"whatsapp:{sender_phone}"} if via == "sender_key" else {})
        key = kwargs.get("conversation_id") or kwargs.get("sender_key") or sender_phone
        before = json.loads(json.dumps(self.states.get(key, {})))
        from app.commerce_context import CommerceConversationState
        from app.commerce.turn_resolver import resolve_commerce_turn
        before_state = CommerceConversationState.from_payload(before)
        expected_resolution = resolve_commerce_turn(text, state=before_state)
        call_start, handler_start = len(self.calls), len(self.handlers)
        result = await process_incoming_message(
            IncomingMessage(channel="whatsapp", provider="ycloud", text=text,
                            sender_phone=sender_phone, **kwargs), {})
        after = json.loads(json.dumps(self.states.get(key, {})))
        self.history.setdefault(key, []).extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": result.reply_text},
        ])
        meta = result.response_metadata or {}
        row = {
            "input": redact_text(text), "identity": identity, "intent": result.intent,
            "active_topic": after.get("active_topic"),
            "dialogue_phase": (meta.get("dialogue") or {}).get("phase"),
            "pending_before": before.get("pending_action"),
            "pending_after": after.get("pending_action"),
            "resolver_action": expected_resolution.action,
            "active_product_before": before_state.active_product.product_id if before_state.active_product else None,
            "presented_before": [item.product_id for item in before_state.last_presented_products],
            "cart_before": [(item.product_id, item.quantity) for item in before_state.cart_items],
            "purchase_stage_before": before_state.purchase_stage,
            "previous_active_before": before_state.previous_active_product.product_id if before_state.previous_active_product else None,
            "last_catalog_query_before": before_state.last_catalog_query,
            "open_order_before": bool(before_state.order_id or before_state.order_lookup_id),
            "handler": list(dict.fromkeys(self.handlers[handler_start:])),
            "tools": [name for name, _ in self.calls[call_start:]],
            "status_before": (before.get("customer_registration") or {}).get("status"),
            "status_after": (after.get("customer_registration") or {}).get("status"),
            "handoff_required": result.handoff_required,
            "draft_fields_before": sorted((before.get("customer_registration") or {}).get("draft", {})),
            "draft_fields_after": sorted((after.get("customer_registration") or {}).get("draft", {})),
            "response_source": meta.get("response_source"),
            "fallback_reason": meta.get("fallback_reason"),
            "safety_reason": result.safety_reason,
            "factual_fallback": (meta.get("factual_validation") or {}).get("fallback_applied", False),
            "reply": redact_text(result.reply_text),
        }
        self.rows.append(row)
        return result, row


@pytest.mark.asyncio
async def test_replay_registration_persists_each_request_and_survives_commerce_detour(monkeypatch):
    replay = Replay(monkeypatch)
    _, start = await replay.say("quero me cadastrar")
    assert start["pending_after"] == "awaiting_customer_registration_data"
    _, doc = await replay.say("52998224725")
    assert "nome" in doc["reply"].casefold()
    _, detour = await replay.say("tem relógio masculino?")
    assert detour["pending_after"] == "awaiting_customer_registration_data"
    _, name = await replay.say("João Teste da Silva")
    assert "e-mail" in name["reply"].casefold()
    _, email = await replay.say("joao.teste@example.com")
    assert email["pending_after"] == "awaiting_customer_registration_confirmation"
    _, created = await replay.say("confirmo o cadastro")
    assert created["status_after"] == "created"
    assert [name for name, _ in replay.calls if name == "create_customer"] == ["create_customer"]
    assert all(not row["factual_fallback"] for row in replay.rows)


@pytest.mark.asyncio
async def test_long_conversation_switches_topics_and_resumes_registration(monkeypatch):
    replay = Replay(monkeypatch)
    messages = [
        "oi", "tem relógio masculino?", "quero um relógio preto",
        "tem até 1000 reais?", "qual o mais barato?", "e o segundo?",
        "quanto custa?", "tem estoque?", "não esse", "me mostra outro",
        "quero me cadastrar", "52998224725", "João Teste da Silva",
        "quanto custa aquele relógio?", "joao.teste@example.com",
        "ok", "confirmo o cadastro", "quero ver relógios",
        "qual o preço do WATCH-1?", "tem preto?", "como faço o pagamento?",
        "quero falar com uma pessoa",
    ]
    for message in messages:
        await replay.say(message)
    assert len(replay.rows) == 22
    assert replay.rows[10]["pending_after"] == "awaiting_customer_registration_data"
    assert replay.rows[13]["pending_after"] == "awaiting_customer_registration_data"
    assert replay.rows[14]["pending_after"] == "awaiting_customer_registration_confirmation"
    assert replay.rows[15]["tools"] == []
    assert replay.rows[16]["status_after"] == "created"
    assert len([name for name, _ in replay.calls if name == "create_customer"]) == 1
    assert all(not replay.rows[index]["factual_fallback"] for index in (10, 11, 12, 14, 15, 16))
    assert replay.rows[-1]["handoff_required"] is True
    assert "search_products" not in replay.rows[-1]["tools"]


@pytest.mark.asyncio
async def test_long_conversation_filter_reject_cart_checkout_registration_and_back(monkeypatch):
    """produto -> filtro -> comparacao -> rejeicao -> outro -> carrinho -> pagamento
    -> cadastro -> retorno ao carrinho, sem perder contexto nem cair em fallback
    generico em nenhum turno."""
    replay = Replay(monkeypatch)
    messages = [
        "tem relógio masculino?", "quero um relógio preto", "tem até 1000 reais?",
        "tem relógio?", "tem preto?", "qual o mais barato?", "não esse",
        "me mostra outro", "coloca no carrinho", "quero dois", "quero pagar",
        "quero me cadastrar", "52998224725", "João Teste da Silva",
        "joao.teste@example.com", "confirmo o cadastro", "vamos fechar",
        "meu carrinho",
    ]
    generic_fallback = "Qual característica ou preferência é mais importante para você?"
    for message in messages:
        await replay.say(message)
    assert all(row["reply"] != generic_fallback for row in replay.rows)
    assert not any(
        row["reply"] == previous["reply"]
        for previous, row in zip(replay.rows, replay.rows[1:])
    )
    state = replay.states["wa:5511988880001"]
    assert [(item["product_id"], item["quantity"]) for item in state["cart_items"]] == [("watch-1", 2)]
    assert state["customer_registration"]["status"] == "created"
    reject_row, other_row, checkout_row, cart_row = (
        replay.rows[6], replay.rows[7], replay.rows[-2], replay.rows[-1]
    )
    assert "Relógio Prata Clássico" in reject_row["reply"]
    assert "Relógio Preto Esportivo" in other_row["reply"]
    assert "condição de pagamento" in checkout_row["reply"].casefold()
    assert "2" in cart_row["reply"] and "Relógio Preto Esportivo" in cart_row["reply"]


@pytest.mark.asyncio
async def test_two_customers_and_sender_identity_do_not_mix_registration(monkeypatch):
    replay = Replay(monkeypatch)
    for identity, via, document, name in (
        ("a", "conversation_id", "52998224725", "Joao Teste da Silva"),
        ("b", "sender_key", "11144477735", "Ana Teste da Silva"),
    ):
        await replay.say("quero me cadastrar", identity=identity, via=via)
        await replay.say(document, identity=identity, via=via)
        await replay.say(name, identity=identity, via=via)
    a = replay.states["wa:5511988880001"]["customer_registration"]
    b = replay.states["whatsapp:5511988880002"]["customer_registration"]
    assert a["draft"]["document"] == "52998224725"
    assert b["draft"]["document"] == "11144477735"
    assert a["draft"]["legal_name"] != b["draft"]["legal_name"]
    assert all("52998224725" not in row["input"] for row in replay.rows)


@pytest.mark.asyncio
async def test_greetings_do_not_repeat_when_prior_replies_are_loaded(monkeypatch):
    replay = Replay(monkeypatch)
    for message in ("oi", "bom dia", "tudo bem?"):
        await replay.say(message)
    assert len({row["reply"] for row in replay.rows}) == 3
    assert all(row["intent"] == "general" for row in replay.rows)


async def _complete_pf_review(replay):
    for message in ("quero me cadastrar", "52998224725", "Joao Teste da Silva", "joao@example.com"):
        await replay.say(message)
    assert replay.rows[-1]["pending_after"] == "awaiting_customer_registration_confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup,status", [
    ({"ok": True, "found": True, "customer_id": "123"}, "linked"),
    ({"ok": False, "status": "AMBIGUOUS"}, "ambiguous"),
    ({"ok": False, "status": "CREATION_PENDING"}, "creation_pending"),
])
async def test_lookup_barriers_never_post_in_real_pipeline(monkeypatch, lookup, status):
    replay = Replay(monkeypatch, lookup=lookup)
    await _complete_pf_review(replay)
    _, row = await replay.say("confirmo o cadastro")
    assert row["status_after"] == status
    assert row["pending_after"] is None
    assert row["tools"] == ["lookup_customer_by_document"]


@pytest.mark.asyncio
async def test_pending_sync_survives_request_restart_without_second_post(monkeypatch):
    replay = Replay(monkeypatch, create={"ok": True, "status": "CREATED_PENDING_SYNC"})
    await _complete_pf_review(replay)
    _, pending = await replay.say("confirmo o cadastro")
    assert pending["status_after"] == "created_pending_sync"
    assert pending["tools"] == ["lookup_customer_by_document", "create_customer"]
    saved_state = json.loads(json.dumps(replay.states))
    restarted = Replay(monkeypatch, lookup={"ok": False, "status": "CREATION_PENDING"})
    restarted.states = saved_state
    _, still_pending = await restarted.say("quero me cadastrar")
    assert still_pending["tools"] == ["lookup_customer_by_document"]
    assert still_pending["status_after"] == "created_pending_sync"
    restarted.lookup = {"ok": True, "found": True, "customer_id": "123"}
    _, linked = await restarted.say("quero me cadastrar")
    assert linked["status_after"] == "created"
    assert linked["tools"] == ["lookup_customer_by_document"]
    assert all(name != "create_customer" for name, _ in restarted.calls)


@pytest.mark.asyncio
async def test_422_email_requires_new_review_and_confirmation(monkeypatch):
    replay = Replay(monkeypatch, create={"ok": False, "code": "customer_validation", "fields": ["emails"]})
    await _complete_pf_review(replay)
    _, rejected = await replay.say("confirmo o cadastro")
    assert rejected["pending_after"] == "awaiting_customer_registration_data"
    assert "email" not in rejected["draft_fields_after"]
    assert "e-mail" in rejected["reply"].casefold()
    replay.create = {"ok": True, "customer_id": "777"}
    _, corrected = await replay.say("novo@example.com")
    assert corrected["pending_after"] == "awaiting_customer_registration_confirmation"
    assert corrected["tools"] == []
    _, created = await replay.say("confirmo o cadastro")
    assert created["status_after"] == "created"
    assert [name for name, _ in replay.calls].count("create_customer") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_error", [
    {"ok": False, "code": "customer_creation_unknown"},
    {"ok": False, "code": "mutation_state_unknown"},
])
async def test_ambiguous_creation_result_never_reposts(monkeypatch, creation_error):
    replay = Replay(monkeypatch, create=creation_error)
    await _complete_pf_review(replay)
    _, unknown = await replay.say("confirmo o cadastro")
    assert unknown["status_after"] == "unknown"
    _, repeated = await replay.say("quero me cadastrar")
    assert repeated["tools"] == []
    assert [name for name, _ in replay.calls].count("create_customer") == 1


@pytest.mark.asyncio
async def test_email_correction_on_review_invalidates_prior_confirmation(monkeypatch):
    replay = Replay(monkeypatch)
    await _complete_pf_review(replay)
    for weak_confirmation in ("ok", "beleza", "pode ser"):
        _, row = await replay.say(weak_confirmation)
        assert row["tools"] == []
        assert row["pending_after"] == "awaiting_customer_registration_confirmation"
    _, corrected = await replay.say("meu email esta errado, e outro@example.com")
    assert corrected["tools"] == []
    assert corrected["pending_after"] == "awaiting_customer_registration_confirmation"
    assert replay.states["wa:5511988880001"]["customer_registration"]["draft"]["email"] == "outro@example.com"
    _, confirmed = await replay.say("sim, confirmo")
    assert confirmed["tools"] == ["lookup_customer_by_document", "create_customer"]


@pytest.mark.asyncio
async def test_single_message_registration_data_skips_collected_questions(monkeypatch):
    replay = Replay(monkeypatch)
    await replay.say("quero me cadastrar")
    _, row = await replay.say(
        "meu cpf e 52998224725, me chamo Joao Teste da Silva, email joao@example.com"
    )
    assert row["pending_after"] == "awaiting_customer_registration_confirmation"
    assert {"document", "legal_name", "email", "phone"} <= set(row["draft_fields_after"])
    assert row["tools"] == []


@pytest.mark.asyncio
async def test_single_product_category_routes_to_catalog(monkeypatch):
    replay = Replay(monkeypatch)
    _, row = await replay.say("relógio")
    assert row["intent"] == "commerce"
    assert "search_products" in row["tools"]


@pytest.mark.asyncio
async def test_broad_120_turn_replay_builds_routing_matrix(monkeypatch):
    replay = Replay(monkeypatch)
    scripts = [
        ("greetings", ["oi", "bom dia", "boa tarde", "tudo bem?", "olá", "oi",
                       "bom dia", "boa noite", "oi tudo bem", "boa tarde"]),
        ("search", ["tem relógio masculino?", "quero um relógio preto", "tem até 1000 reais?",
                    "quero um mais esportivo", "tem relógio?", "tem preto?",
                    "qual o mais barato?", "e aquele segundo?", "quanto custa?", "tem estoque?"]),
        ("product_change", ["tem relógio masculino?", "não esse", "me mostra outro",
                            "tem de outra marca?", "tem relógio preto?", "quanto custa?",
                            "tem estoque?", "o segundo", "compara o primeiro e o terceiro",
                            "qual a diferença entre esses dois?"]),
        ("cart", ["tem relógio preto?", "quero esse", "coloca no carrinho", "quero dois",
                  "remove um", "quero pagar", "como faço o pagamento?", "qual condição de pagamento?",
                  "não quero pagar agora", "tem outro relógio?"]),
        ("order", ["já fiz meu pedido", "qual status?", "onde está meu pedido?",
                   "pedido 12345", "cadê meu pedido 12345", "já paguei", "como acompanho?",
                   "qual o prazo?", "quero falar com atendimento humano", "oi"]),
        ("handoff", ["quero falar com uma pessoa", "quero atendimento humano", "oi",
                     "tem relógio?", "não quero cadastrar", "não quero esse produto",
                     "quero falar com atendente", "tudo bem?", "cadastro", "cancela meu cadastro"]),
        ("registration_pf", ["oi", "quero me cadastrar", "meu email é pf@example.com",
                             "52998224725", "João Teste da Silva", "ok", "beleza",
                             "confirmo o cadastro", "tem relógio preto?", "quero me cadastrar"]),
        ("registration_pj", ["quero cadastrar minha empresa", "11222333000181",
                             "Empresa Teste Ltda", "Loja Teste", "pj@example.com",
                             "pode ser", "pode cadastrar", "oi", "tem relógio?", "cadastro"]),
        ("cancel", ["quero me cadastrar", "52998224725", "cancela meu cadastro",
                    "oi", "quero me cadastrar", "meu email é novo@example.com",
                    "11144477735", "Ana Teste da Silva", "não", "cancela meu cadastro"]),
        ("negation", ["não quero cadastrar", "tem relógio?", "não quero esse produto",
                      "não quero pagar agora", "sim", "não", "esse", "o segundo", "preto", "2"]),
        ("mixed", ["relógio", "quero me cadastrar", "52998224725",
                   "quanto custa o relógio?", "João Teste da Silva", "meu email é mix@example.com",
                   "confirmo o cadastro", "quero ver relógios", "onde está meu pedido?", "oi"]),
        ("commerce", ["tem carregador USB-C?", "quanto custa o WATCH-1?", "tem estoque do WATCH-1?",
                       "tem relógio até 1000 reais?", "me mostre relógios", "qual o segundo?",
                       "quero um relógio preto", "tem relógio esportivo?", "quero comprar relógio",
                       "quero falar com uma pessoa"]),
    ]
    for identity, (group, messages) in enumerate(scripts, start=10):
        for message in messages:
            _, row = await replay.say(message, identity=str(identity))
            row["group"] = group
    assert len(replay.rows) == 120
    assert all(not row["factual_fallback"] for row in replay.rows if row["group"].startswith("registration"))
    assert len([name for name, _ in replay.calls if name == "create_customer"]) <= 3
    if os.getenv("CHATBO_WRITE_AUDIT") == "1":
        path = Path(__file__).resolve().parents[1] / "docs" / "chatbo_validation_matrix.md"
        columns = ("group", "input", "intent", "handler", "tools", "resolver_action",
                   "active_product_before", "presented_before", "cart_before",
                   "purchase_stage_before", "previous_active_before", "last_catalog_query_before",
                   "open_order_before", "pending_before",
                   "pending_after", "status_after", "response_source", "fallback_reason",
                   "safety_reason", "reply")
        lines = ["# ChatBô routing matrix — 120 synthetic turns", "",
                 "Generated by `test_broad_120_turn_replay_builds_routing_matrix` with PII redaction.", "",
                 "| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
        for row in replay.rows:
            cells = [str(row.get(key) or "").replace("|", "\\|").replace("\n", " ") for key in columns]
            lines.append("| " + " | ".join(cells) + " |")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
