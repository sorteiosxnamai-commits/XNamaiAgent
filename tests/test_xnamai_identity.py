"""Identidade, canais e limites comerciais da Xnamai."""
from pathlib import Path
import pytest

from app.site_knowledge import SITE_URL, STORE_URL, build_site_knowledge_text


def test_official_channels_are_in_system_instructions_and_persona():
    from app.openai_agent import SYSTEM_INSTRUCTIONS
    persona = (Path(__file__).resolve().parents[1] / "persona_xnamai.txt").read_text(encoding="utf-8")
    for text in (SYSTEM_INSTRUCTIONS, persona, build_site_knowledge_text()):
        assert "xnamai" in text.casefold()
        assert SITE_URL in text
        assert STORE_URL in text
        assert "eletrônicos" in text
        assert "acessórios de celular" in text


def test_history_cannot_override_identity():
    from app.openai_agent import SYSTEM_INSTRUCTIONS
    assert "não é fonte de verdade" in " ".join(SYSTEM_INSTRUCTIONS.casefold().split())
    assert "não a anuncie" in SYSTEM_INSTRUCTIONS


def test_public_knowledge_contains_no_unverified_commercial_terms():
    text = build_site_knowledge_text()
    for term in ("R$", "Lotomania", "Cartão Presente", "+55"):
        assert term not in text
    assert "Não presuma políticas" in text


def test_no_vip_identity_is_preconfigured():
    from app.vip_profiles import VIP_PROFILES
    assert VIP_PROFILES == ()


def test_handoff_uses_xnamai_and_no_invented_contact():
    from app.handoff_service import build_human_handoff_result
    for reason in ("customer_requested_human", "trade_in_or_appraisal"):
        result = build_human_handoff_result(reason=reason)
        assert "xnamai" in result.reply_text.casefold()
        assert "whatsapp" not in result.response_metadata


def test_multimodal_instructions_cover_xnamai_products():
    from app.image_product_id import IMAGE_IDENTIFY_INSTRUCTIONS, ImageProductIdentification, interpretation_from_identification
    from app.product_image_index import VISUAL_FINGERPRINT_INSTRUCTIONS
    from app.story_visual_analyzer import STORY_VISION_INSTRUCTIONS
    for text in (IMAGE_IDENTIFY_INSTRUCTIONS, VISUAL_FINGERPRINT_INSTRUCTIONS, STORY_VISION_INSTRUCTIONS):
        assert "Xnamai" in text
        assert "acessórios de celular" in text
    image = ImageProductIdentification(product_type="carregador", model="PD20", features=["USB-C"], confidence=0.9)
    interpreted = interpretation_from_identification(image)
    assert interpreted.subject.product_type == "carregador"
    assert "USB-C" in interpreted.preferences.attributes


def test_commercial_mutations_remain_unavailable():
    from app.commerce.mercos.provider import LLM_EXPOSED_CAPABILITIES
    assert LLM_EXPOSED_CAPABILITIES == frozenset({"search_products", "get_product", "check_inventory"})


@pytest.mark.parametrize("text", ("cabo USB-C preto", "carregador 20W", "fone Bluetooth até 100", "capas transparentes", "película para celular"))
def test_electronics_categories_route_to_commerce_without_llm(text):
    from app.sales_agent import deterministic_scope
    from app.commerce_router import resolve_commerce_action
    assert deterministic_scope(text)["domain"] == "commerce"
    assert resolve_commerce_action(text) == "product_search"


def test_requested_accessory_category_is_not_rejected_as_an_unrelated_accessory():
    from app.models import SalesInterpretation
    from app.product_retrieval import score_catalog_candidates
    interpretation = SalesInterpretation(domain="commerce", goal="find", subject={"product_type": "fone", "model": "AudioMax"}, preferences={}, references_previous_context=False, needs_clarification=False, confidence=0.9)
    product = {"id": "1", "name": "Fone AudioMax", "model": ""}
    assert score_catalog_candidates([product], interpretation) == [product]


@pytest.mark.parametrize("term", ("Tray", "sorteio", "Cartão Presente", "Lotomania"))
def test_active_prompt_contains_no_inactive_business_rules(term):
    from prompt_surface import render_prompt_surface
    assert not [name for name, text in render_prompt_surface().items() if term in text]
