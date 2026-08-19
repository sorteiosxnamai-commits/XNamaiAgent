import pytest

from app.commerce_context import CommerceConversationState, CommerceProductReference
from app.config import get_settings
from app.models import AgentResult, IncomingMessage
from app.response_critique import (
    CRITIQUE_JUDGE_SYSTEM_PROMPT,
    CritiqueVerdict,
    RecommendedApiCall,
    apply_response_critique_loop,
    apply_search_products_to_result,
    _fill_api_arguments,
    _seed_args_from_context,
)
from app.capability_catalog import build_capability_catalog, RETRYABLE_API_NAMES


def _allow_critique_llm_without_risk(monkeypatch):
    """Legacy critique-loop tests exercise the LLM path directly."""
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_LLM_ON_RISK_ONLY", "false")
    get_settings.cache_clear()


def test_capability_catalog_includes_order_payment_apis():
    catalog = build_capability_catalog()
    assert "get_order_payment" in catalog["commerce_apis"]
    assert "get_order_payment" in RETRYABLE_API_NAMES
    assert catalog["policy"]


def test_fill_api_arguments_uses_order_seed():
    seeds = _seed_args_from_context(
        state=CommerceConversationState(order_id="25400"),
        result=AgentResult(reply_text="x", intent="commerce"),
    )
    args = _fill_api_arguments(
        RecommendedApiCall(name="get_order_payment", arguments={}),
        seeds,
    )
    assert args == {"order_id": "25400"}


def test_seed_args_from_active_product_reference():
    seeds = _seed_args_from_context(
        state=CommerceConversationState(
            active_product=CommerceProductReference(
                product_id="9991",
                name="Relógio Christopher Ward C63 Sealander Automático Rosa",
            )
        ),
        result=AgentResult(reply_text="x", intent="commerce"),
    )
    assert seeds["product_id"] == "9991"
    assert "Sealander" in (seeds["query"] or "")


@pytest.mark.asyncio
async def test_critique_enforce_retries_api_and_regenerates(monkeypatch):
    _allow_critique_llm_without_risk(monkeypatch)
    incoming = IncomingMessage(
        channel="whatsapp",
        text="me da o link para pagamento",
    )
    result = AgentResult(
        reply_text="Ainda não há pedido criado nem link de pagamento disponível.",
        intent="commerce",
        commercial_data={},
    )
    state = CommerceConversationState(order_id="25400")
    turns = [
        {
            "role": "assistant",
            "content": (
                "Use este link: https://www.newstorerj.com.br/loja/pagamento.php"
                "?loja=687890&pedido=0CC131B51070AEF"
            ),
        }
    ]
    calls = {"judge": 0, "tools": []}

    async def fake_judge(**kwargs):
        calls["judge"] += 1
        if calls["judge"] == 1:
            return CritiqueVerdict(
                score=20,
                pass_check=False,
                issues=["ignored_existing_payment_link"],
                summary="link exists in transcript",
                recommended_apis=[
                    RecommendedApiCall(
                        name="get_order_payment",
                        arguments={"order_id": "25400"},
                        reason="recover payment url",
                    )
                ],
                retry_instruction="Return the existing payment link",
                better_reply_hint="Send the payment URL from facts",
            )
        return CritiqueVerdict(
            score=95,
            pass_check=True,
            issues=[],
            summary="ok",
        )

    async def fake_execute(name, args):
        calls["tools"].append((name, args))
        return {
            "success": True,
            "payment": {
                "payment_url": (
                    "https://www.newstorerj.com.br/loja/pagamento.php"
                    "?loja=687890&pedido=0CC131B51070AEF"
                ),
                "has_payment": False,
            },
        }

    async def fake_regen(**kwargs):
        regenerated = kwargs["result"].model_copy(deep=True)
        regenerated.reply_text = (
            "Segue o link: https://www.newstorerj.com.br/loja/pagamento.php"
            "?loja=687890&pedido=0CC131B51070AEF"
        )
        regenerated.commercial_data = {
            "order_id": "25400",
            "payment": {
                "payment_url": (
                    "https://www.newstorerj.com.br/loja/pagamento.php"
                    "?loja=687890&pedido=0CC131B51070AEF"
                )
            },
        }
        return regenerated

    monkeypatch.setattr(
        "app.response_critique.run_critique_judge",
        fake_judge,
    )
    monkeypatch.setattr(
        "app.response_critique._regenerate_reply",
        fake_regen,
    )

    final, report = await apply_response_critique_loop(
        incoming=incoming,
        result=result,
        recent_turns=turns,
        commerce_state=state,
        mode="enforce",
        max_retries=2,
        execute=fake_execute,
    )

    assert report.regenerated is True
    assert report.approved is True
    assert "pagamento.php" in final.reply_text
    assert calls["tools"][0][0] == "get_order_payment"
    assert final.response_metadata["response_critique"]["attempts"] == 2


@pytest.mark.asyncio
async def test_critique_shadow_does_not_change_reply(monkeypatch):
    _allow_critique_llm_without_risk(monkeypatch)
    incoming = IncomingMessage(channel="whatsapp", text="me da o link")
    result = AgentResult(reply_text="sem link", intent="commerce")

    async def fake_judge(**kwargs):
        return CritiqueVerdict(
            score=10,
            pass_check=False,
            issues=["bad"],
            summary="bad",
            recommended_apis=[
                RecommendedApiCall(name="get_order_payment", arguments={"order_id": "1"})
            ],
        )

    monkeypatch.setattr("app.response_critique.run_critique_judge", fake_judge)

    final, report = await apply_response_critique_loop(
        incoming=incoming,
        result=result,
        mode="shadow",
        max_retries=2,
        execute=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no tools")),
    )
    assert final.reply_text == "sem link"
    assert report.regenerated is False
    assert report.approved is False


def test_critique_judge_prompt_requires_catalog_fit():
    prompt = CRITIQUE_JUDGE_SYSTEM_PROMPT.casefold()
    assert "cronógrafo" in prompt or "cronografo" in prompt
    assert "search_products" in prompt
    assert "commercial_data.products" in prompt


def test_apply_search_products_replaces_classic_list():
    result = AgentResult(
        reply_text="Encontrei estes Bulova Classic…",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "737", "name": "Bulova Classic Automatic"},
                {"id": "753", "name": "Bulova Classic"},
            ]
        },
        response_metadata={"presented_products": True},
    )
    state = CommerceConversationState()
    updated = apply_search_products_to_result(
        result=result,
        api_facts={
            "search_products": {
                "products": [
                    {
                        "id": "9001",
                        "name": "Bulova Marine Star Chronograph",
                        "brand": "Bulova",
                    }
                ]
            }
        },
        commerce_state=state,
        search_query="cronógrafo",
    )
    products = updated.commercial_data["products"]
    assert len(products) == 1
    assert "Chronograph" in products[0]["name"]
    assert updated.commercial_data["query"] == "cronógrafo"
    assert updated.response_metadata["critique_products_replaced"] is True
    assert state.last_presented_products[0].product_id == "9001"


def test_apply_search_products_empty_clears_wrong_list():
    result = AgentResult(
        reply_text="lista errada",
        intent="commerce",
        commercial_data={
            "products": [{"id": "737", "name": "Bulova Classic Automatic"}],
            "inventory": {"737": True},
        },
    )
    updated = apply_search_products_to_result(
        result=result,
        api_facts={"search_products": {"products": []}},
    )
    assert updated.commercial_data["products"] == []
    assert "inventory" not in updated.commercial_data
    assert updated.response_metadata["presented_products"] is False
    assert updated.response_metadata["product_resolution_state"] == "not_found"


@pytest.mark.asyncio
async def test_critique_catalog_mismatch_retries_search_and_swaps_products(monkeypatch):
    _allow_critique_llm_without_risk(monkeypatch)
    incoming = IncomingMessage(channel="whatsapp", text="quero um chrono")
    result = AgentResult(
        reply_text="Separei 3 opções Bulova Classic…",
        intent="commerce",
        commercial_data={
            "products": [
                {"id": "737", "name": "Bulova Classic Automatic"},
                {"id": "753", "name": "Bulova Classic"},
                {"id": "783", "name": "Bulova Classic Dress"},
            ]
        },
        response_metadata={"presented_products": True},
    )
    state = CommerceConversationState()
    calls = {"judge": 0, "tools": []}

    async def fake_judge(**kwargs):
        calls["judge"] += 1
        products = (kwargs["result"].commercial_data or {}).get("products") or []
        names = " ".join(str(p.get("name") or "") for p in products if isinstance(p, dict))
        if "Chronograph" not in names and "Cronógrafo" not in names:
            return CritiqueVerdict(
                score=25,
                pass_check=False,
                issues=["catalog_fit_mismatch"],
                summary="Classic Automatic ≠ cronógrafo",
                recommended_apis=[
                    RecommendedApiCall(
                        name="search_products",
                        arguments={"query": "cronógrafo", "limit": 5},
                        reason="refine for chronograph function",
                    )
                ],
                retry_instruction="Buscar cronógrafos e apresentar só itens com evidência",
            )
        return CritiqueVerdict(score=95, pass_check=True, issues=[], summary="ok")

    async def fake_execute(name, args):
        calls["tools"].append((name, args))
        assert name == "search_products"
        assert args["query"] == "cronógrafo"
        return {
            "products": [
                {
                    "id": "9001",
                    "name": "Relógio Bulova Marine Star Chronograph",
                    "brand": "Bulova",
                }
            ]
        }

    async def fake_regen(**kwargs):
        swapped = apply_search_products_to_result(
            result=kwargs["result"],
            api_facts=kwargs["api_facts"],
            commerce_state=kwargs.get("commerce_state"),
            search_query="cronógrafo",
        )
        swapped.reply_text = (
            "Encontrei este cronógrafo: Relógio Bulova Marine Star Chronograph"
        )
        swapped.response_metadata["critique_regenerated"] = True
        return swapped

    monkeypatch.setattr("app.response_critique.run_critique_judge", fake_judge)
    monkeypatch.setattr("app.response_critique._regenerate_reply", fake_regen)

    final, report = await apply_response_critique_loop(
        incoming=incoming,
        result=result,
        commerce_state=state,
        mode="enforce",
        max_retries=1,
        execute=fake_execute,
    )

    assert report.regenerated is True
    assert report.approved is True
    assert calls["tools"][0][0] == "search_products"
    assert final.commercial_data["products"][0]["id"] == "9001"
    assert "Chronograph" in final.reply_text
    assert "Classic" not in final.commercial_data["products"][0]["name"]
    assert state.last_presented_products[0].product_id == "9001"


@pytest.mark.asyncio
async def test_critique_skips_greeting(monkeypatch):
    incoming = IncomingMessage(channel="whatsapp", text="oi")
    result = AgentResult(reply_text="Olá! Como posso ajudar?", intent="greeting")

    async def boom(**_kwargs):
        raise AssertionError("critique judge must not run for greetings")

    monkeypatch.setattr("app.response_critique.run_critique_judge", boom)

    final, report = await apply_response_critique_loop(
        incoming=incoming,
        result=result,
        mode="enforce",
        max_retries=1,
    )
    assert final.reply_text == "Olá! Como posso ajudar?"
    assert report.attempts == 0
    assert final.response_metadata["response_critique"]["skipped"] is True
    assert final.response_metadata["response_critique"]["skip_reason"] == "soft_greeting"


@pytest.mark.asyncio
async def test_critique_generic_catalog_approves_once(monkeypatch):
    """Regression: attribute-free browse still ships after a single approve."""
    _allow_critique_llm_without_risk(monkeypatch)
    incoming = IncomingMessage(channel="whatsapp", text="tem relógio?")
    result = AgentResult(
        reply_text="Tenho estas opções…",
        intent="commerce",
        commercial_data={
            "products": [{"id": "1", "name": "Relógio X"}],
        },
    )
    calls = {"judge": 0}

    async def fake_judge(**_kwargs):
        calls["judge"] += 1
        return CritiqueVerdict(score=90, pass_check=True, issues=[], summary="ok")

    monkeypatch.setattr("app.response_critique.run_critique_judge", fake_judge)

    final, report = await apply_response_critique_loop(
        incoming=incoming,
        result=result,
        mode="enforce",
        max_retries=1,
        execute=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no tools")),
    )
    assert calls["judge"] == 1
    assert report.approved is True
    assert report.regenerated is False
    assert final.reply_text == "Tenho estas opções…"


@pytest.mark.asyncio
async def test_critique_risk_gate_skips_llm_on_low_risk(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_CRITIQUE_LLM_ON_RISK_ONLY", "true")
    monkeypatch.setenv("AGENT_CRITIQUE_SHADOW_SAMPLE_RATE", "0")
    get_settings.cache_clear()

    incoming = IncomingMessage(channel="whatsapp", text="tem relógio?")
    result = AgentResult(
        reply_text="Tenho estas opções…",
        intent="commerce",
        commercial_data={
            "products": [{"id": "1", "name": "Relógio X"}],
        },
    )

    async def boom(**_kwargs):
        raise AssertionError("LLM critique must not run when risk gate skips")

    monkeypatch.setattr("app.response_critique.run_critique_judge", boom)

    final, report = await apply_response_critique_loop(
        incoming=incoming,
        result=result,
        mode="shadow",
        max_retries=1,
    )
    assert final.reply_text == "Tenho estas opções…"
    assert report.attempts == 0
    meta = final.response_metadata["response_critique"]
    assert meta["skipped"] is True
    assert meta["risk_gate"] is True
    assert meta["skip_reason"] == "risk_gate_skip"
