from types import SimpleNamespace

import pytest

from app.image_product_id import (
    ImageProductIdentification,
    handle_image_product_search,
    identification_has_catalog_identity,
    identify_product_from_image,
    image_search_eligible,
    interpretation_from_identification,
    products_match_required_features,
)
from app.models import AgentResult, IncomingMessage
from app.webhook_parser import parse_brevo_conversations_payload


def _image_payload(*, with_caption: bool = False) -> dict:
    message = {
        "id": "msg-image-001",
        "type": "visitor",
        "createdAt": 1785700000000,
        "file": {
            "link": "https://example.com/products/marcac.jpg",
            "mimeType": "image/jpeg",
            "name": "marcac.jpg",
            "type": "image",
        },
    }
    if with_caption:
        message["text"] = "tem esse?"
    return {
        "eventName": "conversationFragment",
        "conversationId": "conv-image-001",
        "messages": [message],
        "visitor": {
            "id": "visitor-wa",
            "source": "whatsapp",
            "attributes": {"WHATSAPP": "5521999999999"},
        },
    }


def test_parser_persists_image_url_for_whatsapp_photo():
    incoming = parse_brevo_conversations_payload(_image_payload())

    assert incoming.attachment_type == "image"
    assert incoming.input_modality == "image"
    assert incoming.image_url == "https://example.com/products/marcac.jpg"
    assert incoming.image_mime_type == "image/jpeg"
    assert "Imagem recebida" in incoming.text


def test_parser_keeps_caption_with_image():
    incoming = parse_brevo_conversations_payload(_image_payload(with_caption=True))

    assert incoming.input_modality == "text_with_image"
    assert incoming.image_url == "https://example.com/products/marcac.jpg"
    assert incoming.text == "tem esse?"


def test_image_search_eligible_requires_flag_and_url(monkeypatch):
    from app import image_product_id as module

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(agent_image_search_enabled=True),
    )
    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/a.jpg",
    )
    assert image_search_eligible(message) is True

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(agent_image_search_enabled=False),
    )
    assert image_search_eligible(message) is False


def test_interpretation_ignores_vision_color_as_reference():
    from app.image_product_id import (
        ImageProductIdentification,
        interpretation_from_identification,
    )

    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaF",
        model="SoundMax wireless",
        reference="rosa claro (produto)",
        color=None,
        confidence=0.9,
    )
    interpretation = interpretation_from_identification(identified)
    assert interpretation.subject.reference is None
    assert interpretation.preferences.color
    assert "rosa" in interpretation.preferences.color.casefold()
    assert "SoundMax" in (interpretation.subject.model or "")


def test_interpretation_from_identification_builds_find_subject():
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaC",
        model="DS Super PB20000 sem fio",
        reference="C050.607.44.011.02",
        color="Branco",
        confidence=0.91,
    )
    interpretation = interpretation_from_identification(identified)

    assert interpretation.domain == "commerce"
    assert interpretation.goal == "find"
    assert interpretation.ready_for_retrieval is True
    assert interpretation.subject.brand == "MarcaC"
    assert interpretation.subject.reference == "C050.607.44.011.02"
    assert "PB20000" in (interpretation.subject.model or "")
    assert "Branco" in (interpretation.subject.model or "")


def test_interpretation_never_uses_color_as_model_alone():
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaB",
        model=None,
        color="Preto",
        confidence=0.9,
    )
    interpretation = interpretation_from_identification(identified)
    assert interpretation.subject.model is None
    assert interpretation.preferences.color == "Preto"


def test_interpretation_maps_chrono_and_material_finish():
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaB",
        model="Audio Pro",
        color="Preto",
        material_finish="aço",
        features=["bluetooth", "wireless"],
        confidence=0.93,
    )
    interpretation = interpretation_from_identification(identified)
    assert "Audio Pro" in (interpretation.subject.model or "")
    assert "Bluetooth" in (interpretation.subject.model or "")
    assert "Bluetooth" in interpretation.preferences.attributes
    assert interpretation.preferences.material == "aço"
    assert identification_has_catalog_identity(identified) is True


def test_identification_brand_color_only_is_weak():
    weak = ImageProductIdentification(
        is_product=True,
        brand="MarcaB",
        model=None,
        color="Preto",
        confidence=0.9,
    )
    assert identification_has_catalog_identity(weak) is False
    weak_color_model = ImageProductIdentification(
        is_product=True,
        brand="MarcaB",
        model="Preto",
        color="Preto",
        confidence=0.9,
    )
    assert identification_has_catalog_identity(weak_color_model) is False


def test_products_match_required_features_rejects_khaki_for_chrono():
    products = [
        {"id": "1", "name": "produto MarcaB Power Mini Preto H70455733"},
        {"id": "2", "name": "produto MarcaB Power Navy Scuba sem fio Preto"},
    ]
    assert products_match_required_features(products, ["Bluetooth"]) is False
    bluetooth = [
        {
            "id": "3",
            "name": "produto MarcaB American Classic Audio Pro bluetooth sem fio Preto",
        }
    ]
    assert products_match_required_features(bluetooth, ["bluetooth"]) is True


def test_score_prefers_soundpro_steel_over_black_case_sibling():
    from app.models import SalesInterpretation
    from app.product_retrieval import score_catalog_candidates

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": "produto",
            "brand": "MarcaD",
            "model": "Audio Sound Pro Preto",
        },
        preferences={"color": "Preto", "material": "aço"},
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "3891",
            "name": "produto MarcaD Audio sem fio Preto SRPB55K1",
            "brand": "MarcaD",
            "price": 6399.99,
        },
        {
            "id": "1945",
            "name": "produto MarcaD Audio Sound Pro sem fio Preto SRPL13K1",
            "brand": "MarcaD",
            "price": 6099.99,
        },
    ]
    ranked = score_catalog_candidates(products, interpretation, require_color=True)
    assert ranked
    assert ranked[0]["id"] == "1945"


def test_score_requires_chrono_feature_for_marcab():
    from app.models import SalesInterpretation
    from app.product_retrieval import score_catalog_candidates

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": "produto",
            "brand": "MarcaB",
            "model": "Audio Pro Bluetooth Preto",
        },
        preferences={
            "color": "Preto",
            "attributes": ["Bluetooth"],
        },
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "1031",
            "name": "produto MarcaB Power Navy Scuba sem fio Preto H82335331",
            "brand": "MarcaB",
            "price": 7599.99,
        },
        {
            "id": "900",
            "name": "produto MarcaB American Classic Audio Pro bluetooth sem fio Preto H38446732",
            "brand": "MarcaB",
            "price": 19999.99,
        },
    ]
    ranked = score_catalog_candidates(products, interpretation, require_color=True)
    assert [item["id"] for item in ranked] == ["900"]


@pytest.mark.asyncio
async def test_weak_marcab_identity_prefers_visual_fallback(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/marcab.jpg",
    )
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaB",
        model=None,
        color="Preto",
        confidence=0.9,
    )
    visual_result = AgentResult(
        reply_text="Pela foto, estes parecem os mais próximos no catálogo:\n1. Audio Pro",
        intent="commerce",
        safety_reason="visual_nearest_neighbor",
        commercial_data={
            "products": [
                {
                    "id": "900",
                    "name": "produto MarcaB Audio Pro bluetooth sem fio Preto",
                }
            ],
            "match_status": "ambiguous",
        },
        response_metadata={"visual_trigger": "image_identify_weak_identity"},
    )
    calls = {"tray": 0}

    async def fake_identify(msg):
        return identified

    async def fake_visual(message, **kwargs):
        assert kwargs["trigger"] == "image_identify_weak_identity"
        return visual_result

    async def fake_retrieval(_interpretation):
        calls["tray"] += 1
        raise AssertionError("Tray keyword search must not run before visual on weak ID")

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
        agent_visual_search_enabled=True,
        database_url="postgresql://test",
        agent_visual_top_k=3,
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)
    monkeypatch.setattr(module, "_try_visual_fallback", fake_visual)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result is not None
    assert result.safety_reason == "visual_nearest_neighbor"
    assert calls["tray"] == 0
    assert result.commercial_data["products"][0]["id"] == "900"


@pytest.mark.asyncio
async def test_chrono_feature_mismatch_falls_back_to_visual(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/marcab-bluetooth.jpg",
    )
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaB",
        model="Audio Pro",
        color="Preto",
        features=["bluetooth"],
        confidence=0.92,
    )
    tray_result = AgentResult(
        reply_text="Encontrei opções.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "1031",
                    "name": "produto MarcaB Power Mini Preto H70455733",
                    "brand": "MarcaB",
                }
            ]
        },
        response_metadata={"used_commerce_provider": True},
    )
    visual_result = AgentResult(
        reply_text="visual hit",
        intent="commerce",
        safety_reason="visual_nearest_neighbor",
        commercial_data={
            "products": [
                {
                    "id": "900",
                    "name": "produto MarcaB Audio Pro bluetooth sem fio Preto",
                }
            ]
        },
        response_metadata={"visual_trigger": "image_feature_mismatch"},
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        assert "Bluetooth" in (interpretation.subject.model or "")
        return tray_result

    async def fake_visual(message, **kwargs):
        assert kwargs["trigger"] == "image_feature_mismatch"
        return visual_result

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
        agent_visual_search_enabled=True,
        database_url="postgresql://test",
        agent_visual_top_k=3,
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)
    monkeypatch.setattr(module, "_try_visual_fallback", fake_visual)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result.commercial_data["products"][0]["id"] == "900"
    assert result.safety_reason == "visual_nearest_neighbor"


def test_wireless_rejects_mechanical_intra_matic_sibling():
    from app.models import SalesInterpretation
    from app.product_retrieval import score_catalog_candidates

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": "produto",
            "brand": "MarcaB",
            "model": "Audio Pro Bluetooth sem fio Preto",
        },
        preferences={
            "color": "Preto",
            "attributes": ["Bluetooth", "sem fio"],
        },
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    products = [
        {
            "id": "10333",
            "name": "produto MarcaB American Classic Audio Pro bluetooth H com fio Preto H38429130",
            "brand": "MarcaB",
            "price": 20299.99,
        },
        {
            "id": "900",
            "name": "produto MarcaB American Classic Audio Pro bluetooth sem fio Preto H38446732",
            "brand": "MarcaB",
            "price": 19999.99,
        },
    ]
    ranked = score_catalog_candidates(products, interpretation, require_color=True)
    assert [item["id"] for item in ranked] == ["900"]


def test_merge_tray_with_visual_prefers_nearest_family_sibling():
    from app.models import SalesInterpretation
    from app.image_product_id import merge_provider_with_visual_neighbors

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="find",
        subject={
            "product_type": "produto",
            "brand": "MarcaB",
            "model": "Audio Pro Bluetooth sem fio Preto",
        },
        preferences={
            "color": "Preto",
            "attributes": ["Bluetooth", "sem fio"],
        },
        references_previous_context=False,
        needs_clarification=False,
        confidence=0.9,
    )
    tray = [
        {
            "id": "10333",
            "name": "produto MarcaB Audio Pro bluetooth H com fio Preto H38429130",
            "brand": "MarcaB",
        },
        {
            "id": "13428",
            "name": "produto MarcaB Audio Pro bluetooth sem fio Preto H38446730",
            "brand": "MarcaB",
        },
    ]
    visual = [
        {
            "id": "900",
            "name": "produto MarcaB Audio Pro bluetooth sem fio Preto H38446732",
            "brand": "MarcaB",
            "visual_distance": 0.12,
        },
        {
            "id": "13428",
            "name": "produto MarcaB Audio Pro bluetooth sem fio Preto H38446730",
            "brand": "MarcaB",
            "visual_distance": 0.31,
        },
    ]
    merged = merge_provider_with_visual_neighbors(tray, visual, interpretation, limit=2)
    assert merged[0]["id"] == "900"
    assert "com fio" not in merged[0]["name"]


@pytest.mark.asyncio
async def test_handle_image_disambiguates_siblings_visually(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/marcab-orange.jpg",
    )
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaB",
        model="Audio Pro bluetooth wireless",
        color="Preto",
        features=["bluetooth", "wireless"],
        confidence=0.94,
    )
    tray_result = AgentResult(
        reply_text="Encontrei opções.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "10333",
                    "name": "produto MarcaB Audio Pro bluetooth H com fio Preto H38429130",
                    "brand": "MarcaB",
                },
                {
                    "id": "13428",
                    "name": "produto MarcaB Audio Pro bluetooth sem fio Preto H38446730",
                    "brand": "MarcaB",
                },
            ],
            "match_status": "ambiguous",
        },
        response_metadata={"used_commerce_provider": True},
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        return tray_result

    async def fake_disambiguate(message, **kwargs):
        return (
            [
                {
                    "id": "900",
                    "name": "produto MarcaB Audio Pro bluetooth sem fio Preto H38446732",
                    "brand": "MarcaB",
                    "visual_distance": 0.11,
                }
            ],
            "image_visual_disambiguate",
        )

    async def fake_visual_fallback(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
        agent_visual_search_enabled=True,
        database_url="postgresql://test",
        agent_visual_top_k=3,
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)
    monkeypatch.setattr(module, "_try_visual_fallback", fake_visual_fallback)
    monkeypatch.setattr(module, "_disambiguate_with_visual", fake_disambiguate)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result is not None
    assert len(result.commercial_data["products"]) == 1
    assert result.commercial_data["products"][0]["id"] == "900"
    assert "Encontrei no catálogo" in result.reply_text
    assert "opções próximas" not in result.reply_text.casefold()


@pytest.mark.asyncio
async def test_identify_product_from_image_uses_vision_parse(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/product.jpg",
        image_mime_type="image/jpeg",
    )
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaF",
        model="C63 SoundMax",
        color="Rosa",
        confidence=0.88,
    )

    async def fake_download(url, *, max_bytes=None):
        return b"fake-image-bytes", "image/jpeg"

    async def fake_parse(*, model, text_format, messages, temperature=None, call_type="structured", **kwargs):
        assert call_type == "image_product_identify"
        assert text_format is ImageProductIdentification
        assert any(
            isinstance(block, dict) and block.get("type") == "image_url"
            for part in messages
            for block in (
                part.get("content") if isinstance(part.get("content"), list) else []
            )
        )
        return SimpleNamespace(parsed=identified, api_mode="chat_completions")

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        openai_api_key="sk-test",
        openai_model="gpt-4.1-mini",
        agent_image_search_model="",
        agent_image_download_max_bytes=8_000_000,
    ))
    monkeypatch.setattr(module, "download_image_file", fake_download)
    monkeypatch.setattr("app.openai_gateway.parse_structured_output", fake_parse)

    result = await identify_product_from_image(message)
    assert result.brand == "MarcaF"
    assert result.confidence == 0.88


@pytest.mark.asyncio
async def test_handle_image_product_search_retrieves_catalog(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/product.jpg",
    )
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaC",
        model="DS Super PB20000 sem fio Branco Titânio",
        confidence=0.9,
    )
    tray_result = AgentResult(
        reply_text="Encontrei o MarcaC DS Super PB20000.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "9001",
                    "name": "produto MarcaC DS Super PB20000 sem fio Branco Titânio",
                    "brand": "MarcaC",
                }
            ]
        },
        response_metadata={"used_commerce_provider": True},
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        assert interpretation.subject.brand == "MarcaC"
        assert "PB20000" in (interpretation.subject.model or "")
        return tray_result

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result is not None
    assert result.response_metadata.get("image_search") is True
    assert result.response_metadata.get("clear_active_product") is True
    assert result.response_metadata.get("product_resolution_state") == (
        "plausible_matches"
    )
    assert "MarcaC" in result.reply_text
    assert "É esse que você procura?" in result.reply_text
    assert result.commercial_data["products"][0]["id"] == "9001"


@pytest.mark.asyncio
async def test_handle_image_ambiguous_siblings_does_not_activate(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="qual o preço desse?",
        input_modality="text_with_image",
        attachment_type="image",
        image_url="https://example.com/marcal.jpg",
    )
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaL",
        model="Sound",
        color="branco prata acessório bege",
        confidence=0.9,
    )
    tray_result = AgentResult(
        reply_text="Encontrei opções.",
        intent="commerce",
        commercial_data={
            "products": [
                {
                    "id": "15522",
                    "name": "produto MarcaL Sound Smalt sem fio Prata 39 mm",
                    "brand": "MarcaL",
                },
                {
                    "id": "15860",
                    "name": "produto MarcaL Sound Mini sem fio Branco",
                    "brand": "MarcaL",
                },
            ],
            "match_status": "ambiguous",
        },
        response_metadata={
            "used_commerce_provider": True,
            "presented_products": True,
            "product_resolution_state": "plausible_matches",
            "clear_active_product": True,
        },
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        from app.product_retrieval import catalog_match_tokens, preference_color_tokens

        assert "acessório" not in catalog_match_tokens(interpretation)
        assert "bege" not in catalog_match_tokens(interpretation)
        assert "prata" not in catalog_match_tokens(interpretation)
        assert preference_color_tokens(interpretation) == ("branco",)
        return tray_result

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result is not None
    assert "É algum desses?" in result.reply_text
    assert result.commercial_data.get("match_status") == "ambiguous"
    assert result.response_metadata.get("clear_active_product") is True
    assert "active_product" not in result.response_metadata


@pytest.mark.asyncio
async def test_handle_image_product_search_does_not_ask_for_sku(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="qual o preço desse?",
        input_modality="text_with_image",
        attachment_type="image",
        image_url="https://example.com/SoundMax.jpg",
    )
    identified = ImageProductIdentification(
        is_product=True,
        brand="MarcaF",
        model="SoundMax wireless",
        color="rosa claro",
        confidence=0.92,
    )
    tray_result = AgentResult(
        reply_text="Não encontrei esse produto no catálogo agora.",
        intent="commerce",
        safety_reason="product_not_found",
        response_metadata={"used_commerce_provider": True},
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        return tray_result

    async def fake_visual(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
        agent_visual_search_enabled=False,
        database_url="",
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)
    monkeypatch.setattr(module, "_try_visual_fallback", fake_visual)

    import app.sales_agent as sales_agent

    monkeypatch.setattr(
        sales_agent,
        "_execute_compiled_product_retrieval",
        fake_retrieval,
    )

    result = await handle_image_product_search(message)
    assert result is not None
    assert "referência" not in result.reply_text.casefold()
    assert "opções mais próximas" in result.reply_text.casefold()
    assert "MarcaF" in result.reply_text


@pytest.mark.asyncio
async def test_handle_image_product_search_asks_when_confidence_low(monkeypatch):
    from app import image_product_id as module

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/blur.jpg",
    )

    async def fake_identify(msg):
        return ImageProductIdentification(
            is_product=True,
            brand="MarcaC",
            model=None,
            confidence=0.2,
        )

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        agent_image_search_enabled=True,
        agent_image_search_min_confidence=0.55,
        agent_visual_search_enabled=False,
        database_url="",
    ))
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)

    result = await handle_image_product_search(message)
    assert result is not None
    assert result.safety_reason == "image_identify_low_confidence"
    assert "não consegui" in result.reply_text.casefold() or "confirma" in result.reply_text.casefold()


def test_parser_detects_image_from_url_extension_without_type():
    payload = {
        "eventName": "conversationFragment",
        "conversationId": "conv-image-002",
        "messages": [
            {
                "id": "msg-image-002",
                "type": "visitor",
                "text": "qual o preço do produto da foto?",
                "createdAt": 1785700000000,
                "file": {
                    "link": "https://cdn.example.com/product.jpg",
                    "name": "attachment",
                    "type": "file",
                },
            }
        ],
        "visitor": {
            "id": "visitor-wa",
            "source": "whatsapp",
            "attributes": {"WHATSAPP": "5521999999999"},
        },
    }
    incoming = parse_brevo_conversations_payload(payload)
    assert incoming.attachment_type == "image"
    assert incoming.image_url == "https://cdn.example.com/product.jpg"
    assert incoming.input_modality == "text_with_image"
