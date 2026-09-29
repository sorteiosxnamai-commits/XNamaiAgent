"""Intent Router: contrato, fronteira e ausencia de implementacao duplicada."""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.sales import intent_router as router

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTER_FILE = REPO_ROOT / "app" / "sales" / "intent_router.py"


# --- contrato -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intent", "action"),
    [
        ("product_search", "product_search"),
        ("recommendation", "product_search"),
        ("product_comparison", "product_search"),
        ("price", "product_price"),
        ("inventory", "product_inventory"),
        ("coupon", "coupon_search"),
        ("clarification", None),
        ("purchase_intent", None),
    ],
)
def test_every_intent_has_a_defined_commerce_action(intent, action):
    result = router.route_sales_intent(intent)
    assert result is not None and result.commerce_action == action


def test_forced_retrieval_only_promotes_clarification():
    promoted = router.route_sales_intent("clarification", force_retrieval=True)
    assert (promoted.intent, promoted.forced_retrieval, promoted.commerce_action) == (
        "recommendation", True, "product_search")
    kept = router.route_sales_intent("price", force_retrieval=True)
    assert (kept.intent, kept.forced_retrieval) == ("price", False)


@pytest.mark.parametrize("unknown", [None, "", "commerce", "raffle", "greeting"])
def test_unknown_intent_is_not_routed(unknown):
    assert router.route_sales_intent(unknown) is None


def test_taxonomy_is_closed_and_consistent():
    """Todo intent tem goal; toda acao comercial vem de um intent da taxonomia."""
    from typing import get_args

    intents = set(get_args(router.SalesIntent))
    assert set(router._GOAL_BY_INTENT) == intents
    assert set(router.COMMERCE_ACTION_BY_INTENT) <= intents
    assert set(router.COMMERCE_ACTION_BY_INTENT.values()) == set(get_args(router.CommerceAction))


@pytest.mark.parametrize(
    ("text", "domain"),
    [("oi", "greeting"), ("tem cabo?", "commerce"), ("qual o horário da loja?", "store_general"),
     ("quem ganhou o jogo?", "out_of_scope")],
)
def test_domain_fallback(text, domain):
    assert router.domain_from_text(text) == domain


def test_guardrail_verdict_is_an_input_not_a_dependency():
    assert router.domain_from_text("preciso daquilo", commerce_inquiry=True) == "commerce"
    assert router.domain_from_text("preciso daquilo") == "out_of_scope"


# --- fronteira -----------------------------------------------------------------

#: O router so classifica: nada de provider, envio, memoria, handoff, persona,
#: checkout, outbox ou LLM.
FORBIDDEN_IMPORT_TOKENS = (
    "commerce.tools", "commerce_router", "mercos", "execute_tool", "outbox", "channels",
    "brevo", "memory", "handoff", "human_takeover", "persona", "prompt", "openai", "checkout",
    "cart_service", "order_service", "payment", "response_presenter", "response_composer", "db",
    "sales_agent", "guardrails",
)


def _imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_router_has_no_commercial_or_persona_dependency():
    imports = _imports(ROUTER_FILE)
    offending = sorted(i for i in imports if any(token in i for token in FORBIDDEN_IMPORT_TOKENS))
    assert not offending, f"intent router atravessou a fronteira: {offending}"
    assert imports <= {
        "__future__", "__future__.annotations", "re", "unicodedata", "dataclasses", "dataclasses.dataclass", "typing",
        "typing.TYPE_CHECKING", "typing.Literal", "app.product_vocabulary",
        "app.product_vocabulary.mentions_product_category", "app.models", "app.models.SalesInterpretation",
    }, imports


def test_router_functions_are_pure_classification():
    """Nenhuma funcao do router e async (nao faz I/O)."""
    tree = ast.parse(ROUTER_FILE.read_text(encoding="utf-8"))
    assert not [n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


# --- sem duplicacao ---------------------------------------------------------------


def _dict_literals_with_keys(path: pathlib.Path, keys: set[str]) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Dict):
            literal_keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if keys <= literal_keys:
                lines.append(node.lineno)
    return lines


def test_sales_agent_depends_on_the_router_and_keeps_no_intent_tables():
    source = (REPO_ROOT / "app" / "sales_agent.py").read_text(encoding="utf-8")
    assert "from .sales.intent_router import" in source
    for dead in ("def _normalize_semantic_plan", "def _parse_scope", "def _parse_plan", "_ACTION_TO_PLAN",
                 "def _is_greeting"):
        assert dead not in source, dead
    sales = REPO_ROOT / "app" / "sales_agent.py"
    # tabelas intent->acao e goal->intent vivem so no router
    assert not _dict_literals_with_keys(sales, {"price", "inventory", "coupon"})
    assert not _dict_literals_with_keys(sales, {"discover", "find", "recommend", "compare"})


def test_keyword_classifier_has_a_single_implementation():
    source = (REPO_ROOT / "app" / "commerce_router.py").read_text(encoding="utf-8")
    assert "commerce_action_from_text" in source
    assert "cupom comercial" not in source  # termos moraram aqui antes; agora so no router


def test_public_compat_names_still_resolve_through_the_router():
    from app.commerce_router import resolve_commerce_action
    from app.sales_agent import _is_greeting

    assert resolve_commerce_action("quanto custa o cabo?") == router.commerce_action_from_text("quanto custa o cabo?")
    assert _is_greeting is router.is_greeting
