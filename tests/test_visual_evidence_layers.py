"""B10 — "parece X" nao e "e X", e muito menos "X em estoque por R$ Y".

Camadas: hipotese visual -> identificacao (so o cliente confirma) -> produto
de catalogo -> fato comercial (somente o provider comercial).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.image_product_id import ImageProductIdentification, handle_image_product_search
from app.models import AgentResult, IncomingMessage

_ASSERTIVE = ("identifiquei", "é o ", "é a ", "com certeza")


def _message() -> IncomingMessage:
    return IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/fone.jpg",
    )


def _install(monkeypatch, provider_result: AgentResult, *, confidence: float = 0.9):
    from app import image_product_id as module
    import app.sales_agent as sales_agent

    identified = ImageProductIdentification(
        is_product=True, product_type="fone", brand="MarcaB", model="Audio Pro", confidence=confidence
    )

    async def fake_identify(_msg):
        return identified

    async def fake_retrieval(_interpretation):
        return provider_result

    async def no_visual(_message, **_kwargs):
        return None

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
        agent_visual_search_enabled=False,
        database_url="",
        agent_visual_top_k=3,
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)
    monkeypatch.setattr(module, "_try_visual_fallback", no_visual)
    monkeypatch.setattr(sales_agent, "_execute_compiled_product_retrieval", fake_retrieval)


@pytest.mark.asyncio
async def test_hypothesis_without_catalog_match_is_never_stated_as_identification(monkeypatch):
    _install(monkeypatch, AgentResult(reply_text="-", intent="commerce", safety_reason="product_not_found",
                                      commercial_data={"products": []}))
    result = await handle_image_product_search(_message())

    assert "parece" in result.reply_text
    assert not any(word in result.reply_text.casefold() for word in _ASSERTIVE)
    layers = result.response_metadata["visual_evidence"]
    assert layers["hypothesis"]["source"] == "vision"
    assert layers["identification"] == "unconfirmed_until_customer_confirms"
    assert layers["catalog_match"] == "none"
    assert layers["commercial_facts"] == "none"


@pytest.mark.asyncio
async def test_ambiguous_image_asks_which_one_and_activates_nothing(monkeypatch):
    products = [
        {"id": "1", "name": "Fone MarcaB Audio Pro Preto", "brand": "MarcaB"},
        {"id": "2", "name": "Fone MarcaB Audio Pro Branco", "brand": "MarcaB"},
    ]
    _install(monkeypatch, AgentResult(reply_text="-", intent="commerce",
                                      commercial_data={"products": products, "match_status": "ambiguous"},
                                      response_metadata={"used_commerce_provider": True}))
    result = await handle_image_product_search(_message())

    assert "parece" in result.reply_text and "?" in result.reply_text
    assert result.commercial_data["match_status"] == "ambiguous"
    assert result.response_metadata["clear_active_product"] is True  # nenhum SKU ativado
    assert result.response_metadata["product_resolution_state"] == "plausible_matches"
    layers = result.response_metadata["visual_evidence"]
    assert layers["catalog_match"] == "ambiguous"
    assert layers["identification"] == "unconfirmed_until_customer_confirms"


@pytest.mark.asyncio
async def test_commercial_facts_come_only_from_the_provider_payload(monkeypatch):
    """Preco na resposta vem do produto do catalogo, nunca da visao."""
    product = {"id": "7", "name": "Fone MarcaB Audio Pro", "brand": "MarcaB", "current_price": "149.90"}
    _install(monkeypatch, AgentResult(reply_text="-", intent="commerce",
                                      commercial_data={"products": [product], "match_status": "exact"},
                                      response_metadata={"used_commerce_provider": True}))
    result = await handle_image_product_search(_message())

    layers = result.response_metadata["visual_evidence"]
    assert layers["commercial_facts"] == "commerce_provider"
    assert "É esse" in result.reply_text or "?" in result.reply_text  # confirmacao, nao afirmacao
    image_fields = set(result.response_metadata["image_identify"])
    assert not image_fields & {"price", "current_price", "stock", "url"}


@pytest.mark.asyncio
async def test_low_confidence_hypothesis_never_reaches_catalog_assertions(monkeypatch):
    _install(monkeypatch, AgentResult(reply_text="-", intent="commerce"), confidence=0.2)
    result = await handle_image_product_search(_message())

    assert not any(word in result.reply_text.casefold() for word in _ASSERTIVE)
    assert "R$" not in result.reply_text
