from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import IncomingMessage
from app.product_image_index import (
    VisualProductFingerprint,
    build_caption_from_fingerprint,
    index_product_image,
    run_product_image_index_batch,
    search_visual_neighbors,
    source_hash_for_image,
)


def test_source_hash_stable_for_url_only():
    assert source_hash_for_image("https://cdn.example/a.jpg") == source_hash_for_image(
        "https://cdn.example/a.jpg"
    )
    assert source_hash_for_image(
        "https://cdn.example/a.jpg",
        content=b"bytes",
    ) != source_hash_for_image("https://cdn.example/a.jpg")


def test_build_caption_prefers_explicit_caption():
    fingerprint = VisualProductFingerprint(
        brand="Certina",
        model="PH2000M",
        caption="Certina PH2000M branco caixa redonda pulseira titânio",
    )
    assert "branco" in build_caption_from_fingerprint(fingerprint)


def test_search_visual_neighbors_filters_by_max_distance(monkeypatch):
    from app import product_image_index as module

    rows = [
        {
            "product_id": "1",
            "image_url": "https://cdn/a.jpg",
            "brand": "A",
            "model": "M1",
            "reference": None,
            "name": "Watch A",
            "visual_caption": "caption a",
            "distance": 0.2,
        },
        {
            "product_id": "2",
            "image_url": "https://cdn/b.jpg",
            "brand": "B",
            "model": "M2",
            "reference": None,
            "name": "Watch B",
            "visual_caption": "caption b",
            "distance": 0.9,
        },
    ]
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False

    monkeypatch.setattr(module, "get_conn", lambda: conn)
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(agent_visual_top_k=3, agent_visual_max_distance=0.45),
    )

    matches = search_visual_neighbors([0.1] * 8)
    assert len(matches) == 1
    assert matches[0]["product_id"] == "1"


@pytest.mark.asyncio
async def test_index_product_image_skips_unchanged_source_hash(monkeypatch):
    from app import product_image_index as module

    product = {
        "id": "9001",
        "brand": "Certina",
        "name": "Relógio Certina",
        "primary_image_url": "https://cdn.example/certina.jpg",
    }
    image_url = "https://cdn.example/certina.jpg"
    expected_hash = source_hash_for_image(image_url)

    monkeypatch.setattr(module, "official_product_image", lambda p: image_url)
    monkeypatch.setattr(module, "get_indexed_source_hash", lambda pid: expected_hash)

    called = {"fingerprint": False}

    async def boom(*args, **kwargs):
        called["fingerprint"] = True
        raise AssertionError("should skip fingerprint when hash matches")

    monkeypatch.setattr(module, "fingerprint_image_url", boom)

    outcome = await index_product_image(product)
    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "unchanged"
    assert called["fingerprint"] is False


@pytest.mark.asyncio
async def test_index_product_image_upserts_when_hash_changes(monkeypatch):
    from app import product_image_index as module

    product = {
        "id": "9001",
        "brand": "Certina",
        "model": "PH2000M",
        "name": "Relógio Certina PH2000M",
        "primary_image_url": "https://cdn.example/certina.jpg",
    }
    fingerprint = VisualProductFingerprint(
        brand="Certina",
        model="PH2000M",
        dial_color="branco",
        caption="Certina PH2000M mostrador branco caixa redonda",
    )
    upserts: list[dict] = []

    async def fake_fingerprint(url, *, hint=None):
        return fingerprint, "new-hash-abc"

    async def fake_embed(text):
        assert "Certina" in text
        return [0.01] * 8

    def fake_upsert(**kwargs):
        upserts.append(kwargs)

    monkeypatch.setattr(
        module,
        "official_product_image",
        lambda p: "https://cdn.example/certina.jpg",
    )
    monkeypatch.setattr(module, "get_indexed_source_hash", lambda pid: "old-hash")
    monkeypatch.setattr(module, "fingerprint_image_url", fake_fingerprint)
    monkeypatch.setattr(module, "embed_text", fake_embed)
    monkeypatch.setattr(module, "upsert_product_image_index", fake_upsert)

    outcome = await index_product_image(product)
    assert outcome["status"] == "indexed"
    assert len(upserts) == 1
    assert upserts[0]["source_hash"] == "new-hash-abc"
    assert upserts[0]["product_id"] == "9001"


@pytest.mark.asyncio
async def test_run_product_image_index_batch_respects_batch_size(monkeypatch):
    from app import product_image_index as module

    pages_requested: list[dict] = []

    async def fake_tool(name, params):
        assert name == "search_products"
        pages_requested.append(dict(params))
        page = int(params.get("page") or 1)
        limit = int(params.get("limit") or 20)
        # Infinite catalog; batch must stop by scanned count.
        start = (page - 1) * 20
        products = [
            {
                "id": str(start + idx),
                "name": f"Watch {start + idx}",
                "primary_image_url": f"https://cdn/{start + idx}.jpg",
            }
            for idx in range(limit)
        ]
        return {"products": products}

    async def fake_index(product, *, force=False):
        return {"status": "skipped", "reason": "unchanged", "product_id": product["id"]}

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            agent_product_image_index_enabled=True,
            database_url="postgresql://example",
            agent_product_image_index_batch_size=5,
        ),
    )
    monkeypatch.setattr(module, "execute_tool", fake_tool)
    monkeypatch.setattr(module, "index_product_image", fake_index)

    summary = await run_product_image_index_batch(batch_size=5)
    assert summary["ok"] is True
    assert summary["scanned"] == 5
    assert summary["skipped"] == 5
    assert pages_requested[0]["limit"] == 5


@pytest.mark.asyncio
async def test_handle_image_visual_fallback_on_low_confidence(monkeypatch):
    from app import image_product_id as module
    from app.image_product_id import ImageProductIdentification, handle_image_product_search

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/blur.jpg",
    )

    async def fake_identify(msg):
        return ImageProductIdentification(
            is_watch=True,
            brand=None,
            model=None,
            confidence=0.15,
        )

    async def fake_visual(msg, *, identified, trigger):
        assert trigger == "image_identify_low_confidence"
        from app.models import AgentResult

        return AgentResult(
            reply_text="Pela foto, estes parecem os mais próximos no catálogo:\n1. Watch X",
            intent="commerce",
            safety_reason="visual_nearest_neighbor",
            commercial_data={
                "products": [{"id": "42", "name": "Watch X", "brand": "X"}]
            },
            response_metadata={"visual_search": True, "visual_trigger": trigger},
        )

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            agent_image_search_enabled=True,
            agent_image_search_min_confidence=0.55,
            agent_visual_search_enabled=True,
            database_url="postgresql://example",
            agent_visual_top_k=3,
        ),
    )
    monkeypatch.setattr(module, "identify_product_from_image", fake_identify)
    monkeypatch.setattr(module, "_try_visual_fallback", fake_visual)

    result = await handle_image_product_search(message)
    assert result is not None
    assert result.safety_reason == "visual_nearest_neighbor"
    assert "mais próximos" in result.reply_text
    assert result.commercial_data["products"][0]["id"] == "42"


@pytest.mark.asyncio
async def test_handle_image_visual_fallback_when_text_not_found(monkeypatch):
    from app import image_product_id as module
    from app.image_product_id import ImageProductIdentification, handle_image_product_search
    from app.models import AgentResult

    message = IncomingMessage(
        channel="whatsapp",
        text="[Imagem recebida via WhatsApp]",
        input_modality="image",
        attachment_type="image",
        image_url="https://example.com/watch.jpg",
    )
    identified = ImageProductIdentification(
        is_watch=True,
        brand="Certina",
        model="Modelo Inventado XYZ",
        confidence=0.9,
    )

    async def fake_identify(msg):
        return identified

    async def fake_retrieval(interpretation):
        return AgentResult(
            reply_text="Não encontrei",
            intent="commerce",
            safety_reason="product_not_found",
            response_metadata={},
        )

    async def fake_visual(msg, *, identified, trigger):
        assert trigger == "product_not_found"
        return AgentResult(
            reply_text="Pela foto, estes parecem os mais próximos no catálogo:\n1. Certina PH2000M",
            intent="commerce",
            safety_reason="visual_nearest_neighbor",
            commercial_data={
                "products": [{"id": "9001", "name": "Certina PH2000M"}]
            },
            response_metadata={"visual_search": True},
        )

    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            agent_image_search_enabled=True,
            agent_image_search_min_confidence=0.55,
            agent_visual_search_enabled=True,
            database_url="postgresql://example",
            agent_visual_top_k=3,
        ),
    )
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
    assert result.commercial_data["products"][0]["id"] == "9001"
