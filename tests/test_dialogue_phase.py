"""Dialogue Phase: "onde estamos?" — cenarios, transicoes e fronteira."""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.commerce_context import (
    CommerceCartItem,
    CommerceConversationState,
    CommerceProductReference,
    PresentedCommerceProduct,
)
from app.models import SalesInterpretation
from app.sales.dialogue_phase import message_restarts_discovery, phase_from_state, resolve_dialogue_phase
from app.sales.intent_router import intent_from_interpretation, route_sales_intent

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_PRODUCTS = [
    PresentedCommerceProduct(position=1, product_id="10", name="iPhone 13 capa preta", brand="MarcaA"),
    PresentedCommerceProduct(position=2, product_id="11", name="iPhone 13 capa azul", brand="MarcaA"),
    PresentedCommerceProduct(position=3, product_id="12", name="Galaxy capa", brand="MarcaB"),
]
_FOCUS = CommerceProductReference(product_id="11", name="iPhone 13 capa azul")


def _interp(**kw) -> SalesInterpretation:
    base = dict(domain="commerce", references_previous_context=False, needs_clarification=False, confidence=0.9)
    base.update(kw)
    return SalesInterpretation(**base)


def _phase(state, interpretation=None, text=None, handoff=False):
    intent = None
    if interpretation is not None and interpretation.domain == "commerce":
        intent = route_sales_intent(intent_from_interpretation(interpretation))
    return resolve_dialogue_phase(state, intent=intent, interpretation=interpretation,
                                  message_text=text, handoff_active=handoff)


# --- cenarios pedidos --------------------------------------------------------


def test_discovery_quero_um_celular():
    state = _phase(CommerceConversationState(), _interp(goal="discover", subject={"product_type": "celular"}),
                   "quero um celular")
    assert (state.phase, state.state_phase) == ("discovery", "discovery")


def test_shortlist_after_several_options():
    shortlist = CommerceConversationState(last_presented_products=_PRODUCTS)
    state = _phase(shortlist, _interp(goal="inspect", references_previous_context=True,
                                      information_needed=["catalog"]), "tem mais detalhes?")
    assert state.phase == "shortlist"


def test_product_focus_by_position_quanto_custa_o_segundo():
    shortlist = CommerceConversationState(last_presented_products=_PRODUCTS)
    state = _phase(shortlist, _interp(goal="inspect", references_previous_context=True, reference_type="list_position",
                                      reference_position=2, information_needed=["price"]), "quanto custa o segundo?")
    assert (state.phase, state.state_phase, state.transitioned) == ("product_focus", "shortlist", True)


def test_product_focus_by_reference_quero_aquele_iphone():
    shortlist = CommerceConversationState(last_presented_products=_PRODUCTS)
    state = _phase(shortlist, _interp(goal="buy", references_previous_context=True, reference_type="explicit_product",
                                      explicit_product_name="iPhone"), "quero aquele iPhone")
    assert state.phase == "product_focus"


def test_price_question_does_not_define_the_phase_alone():
    """Mesmo intent de preco em estados diferentes -> fases diferentes."""
    price = _interp(goal="inspect", references_previous_context=True, information_needed=["price"])
    assert _phase(CommerceConversationState(), price).phase == "discovery"
    assert _phase(CommerceConversationState(active_product=_FOCUS), price).phase == "product_focus"
    cart = CommerceConversationState(cart_items=[CommerceCartItem(product_id="11", quantity=1)])
    assert _phase(cart, price).phase == "cart"


def test_cart_with_product_added():
    state = CommerceConversationState(active_product=_FOCUS, cart_session_id="c1",
                                      cart_items=[CommerceCartItem(product_id="11", quantity=2)])
    assert _phase(state, _interp(goal="inspect", references_previous_context=True)).phase == "cart"


def test_checkout_is_a_real_phase_between_cart_and_review():
    state = CommerceConversationState(cart_items=[CommerceCartItem(product_id="11", quantity=1)],
                                      pending_action="awaiting_shipping_zipcode")
    assert phase_from_state(state) == "checkout"
    assert phase_from_state(CommerceConversationState(purchase_stage="checkout_ready")) == "checkout"


def test_order_review_waiting_confirmation():
    state = CommerceConversationState(cart_items=[CommerceCartItem(product_id="11", quantity=1)],
                                      order_confirmation_status="pending", order_review_version="v1",
                                      pending_action="awaiting_order_confirmation")
    assert _phase(state, _interp(goal="buy", references_previous_context=True)).phase == "order_review"


def test_after_sale_customer_asks_about_finished_order():
    state = CommerceConversationState(order_id="9001", order_status_group="delivered")
    about_order = _interp(goal="after_sales", references_previous_context=True, order_action="get_order_status")
    assert _phase(state, about_order, "e o meu pedido?").phase == "after_sale"


def test_backtracking_new_product_after_focus():
    focused = CommerceConversationState(active_product=_FOCUS, last_presented_products=_PRODUCTS)
    new_search = _interp(goal="find", subject={"product_type": "carregador"})
    state = _phase(focused, new_search, "agora quero um carregador")
    assert (state.phase, state.state_phase) == ("discovery", "product_focus")


def test_follow_up_on_focused_product_stays_in_focus():
    focused = CommerceConversationState(active_product=_FOCUS)
    follow = _interp(goal="find", subject={"product_type": "capa"}, preferences={"color": "azul"},
                     references_previous_context=True)
    assert _phase(focused, follow, "tem em azul?").phase == "product_focus"


def test_handoff_is_orthogonal_to_the_phase():
    focused = CommerceConversationState(active_product=_FOCUS)
    state = _phase(focused, _interp(goal="inspect", references_previous_context=True), "quero falar com alguém",
                   handoff=True)
    assert (state.phase, state.handoff_active) == ("product_focus", True)


# --- regras de transicao -------------------------------------------------------


@pytest.mark.parametrize("text", ["nenhuma dessas", "Nenhum desses!", "vamos começar de novo", "quero recomeçar"])
def test_explicit_restart_leaves_the_shortlist(text):
    assert message_restarts_discovery(text)
    shortlist = CommerceConversationState(last_presented_products=_PRODUCTS)
    assert _phase(shortlist, _interp(goal="discover", references_previous_context=True), text).phase == "discovery"


def test_open_sale_is_not_regressed_by_browsing():
    cart = CommerceConversationState(cart_items=[CommerceCartItem(product_id="11", quantity=1)])
    assert _phase(cart, _interp(goal="find", subject={"product_type": "fone"}), "tem fone?").phase == "cart"


def test_removing_the_only_cart_item_steps_back_to_the_product():
    cart = CommerceConversationState(active_product=_FOCUS, cart_items=[CommerceCartItem(product_id="11", quantity=1)])
    remove = _interp(goal="buy", references_previous_context=True, purchase_action="remove_cart_item")
    assert _phase(cart, remove, "tira esse").phase == "product_focus"
    two = cart.model_copy(update={"cart_items": [CommerceCartItem(product_id="11", quantity=1),
                                                 CommerceCartItem(product_id="12", quantity=1)]})
    assert _phase(two, remove, "tira esse").phase == "cart"


def test_new_sale_after_an_order_follows_the_browsing_fields():
    after = CommerceConversationState(order_id="9001", last_presented_products=_PRODUCTS)
    pick = _interp(goal="inspect", references_previous_context=True, reference_type="list_position",
                   reference_position=1, information_needed=["price"])
    assert _phase(after, pick, "quanto custa o primeiro?").phase == "product_focus"
    search = _interp(goal="find", subject={"product_type": "cabo"})
    assert _phase(after, search, "tem cabo?").phase == "discovery"


@pytest.mark.parametrize("domain", ["greeting", "store_general", "out_of_scope"])
def test_non_commerce_turn_does_not_move_the_sale(domain):
    cart = CommerceConversationState(cart_items=[CommerceCartItem(product_id="11", quantity=1)])
    state = _phase(cart, _interp(domain=domain), "oi")
    assert (state.phase, state.transitioned) == ("cart", False)


def test_phase_needs_no_interpretation_to_read_the_state():
    assert resolve_dialogue_phase(None).phase == "discovery"
    assert resolve_dialogue_phase(CommerceConversationState(active_product=_FOCUS)).phase == "product_focus"


# --- integracao: observado no turno, sem decidir nada ------------------------------


@pytest.mark.asyncio
async def test_sales_turn_records_phase_and_slots(monkeypatch):
    from types import SimpleNamespace

    import app.sales_agent as sales_agent
    from app.models import AgentResult, IncomingMessage

    async def inner(*_a, **_k):
        return AgentResult(reply_text="ok", intent="commerce")

    monkeypatch.setattr(sales_agent, "_handle_sales_message_inner", inner)
    monkeypatch.setattr(sales_agent, "get_settings", lambda: SimpleNamespace(openai_api_key=""))
    shortlist = CommerceConversationState(last_presented_products=_PRODUCTS,
                                          active_preferences={"budget_max": 1000})
    interpretation = _interp(goal="inspect", references_previous_context=True, reference_type="list_position",
                             reference_position=2, information_needed=["price"], preferences={"budget_max": 500})
    result = await sales_agent.handle_sales_message(
        IncomingMessage(text="quanto custa o segundo? até 500"), {}, {},
        semantic_plan=interpretation, commerce_state=shortlist,
    )
    dialogue = result.response_metadata["dialogue"]
    assert (dialogue["phase"], dialogue["state_phase"], dialogue["handoff_active"]) == (
        "product_focus", "shortlist", False)
    assert dialogue["qualification"]["budget"]["status"] == "known"
    assert "500" not in str(dialogue)  # observacao carrega status/origem, nao valores


# --- fronteira -----------------------------------------------------------------

_FORBIDDEN = ("commerce.tools", "commerce_router", "mercos", "execute_tool", "outbox", "channels", "ycloud",
              "meta_instagram", "brevo", "persona", "prompt", "openai", "db", "memory", "handoff", "sales_agent",
              "response_", "cart_service", "order_service", "payment")


def _imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


@pytest.mark.parametrize("module", ["dialogue_phase.py", "qualification_slots.py"])
def test_new_boundaries_have_no_commercial_outbound_or_persona_dependency(module):
    imports = _imports(REPO_ROOT / "app" / "sales" / module)
    offending = sorted(i for i in imports if any(token in i for token in _FORBIDDEN))
    assert not offending, f"{module}: {offending}"
    tree = ast.parse((REPO_ROOT / "app" / "sales" / module).read_text(encoding="utf-8"))
    assert not [n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]  # sem I/O


def test_intent_router_knows_neither_phase_nor_slots():
    imports = _imports(REPO_ROOT / "app" / "sales" / "intent_router.py")
    assert not [i for i in imports if "dialogue_phase" in i or "qualification" in i]


def test_slots_do_not_depend_on_phase_and_phase_does_not_depend_on_slots():
    """Responsabilidades separadas: nenhum dos dois importa o outro."""
    assert not [i for i in _imports(REPO_ROOT / "app" / "sales" / "qualification_slots.py") if "dialogue" in i]
    assert not [i for i in _imports(REPO_ROOT / "app" / "sales" / "dialogue_phase.py") if "qualification" in i]


def test_neither_module_produces_customer_text():
    """Nenhum dos dois decide 'o que dizer': nada de reply_text/AgentResult."""
    for module in ("dialogue_phase.py", "qualification_slots.py"):
        source = (REPO_ROOT / "app" / "sales" / module).read_text(encoding="utf-8")
        assert "reply_text" not in source and "AgentResult" not in source, module
