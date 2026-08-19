from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.openai_gateway import ResponsesGateway


class ProductHint(BaseModel):
    brand: str
    model: str | None = None


@pytest.mark.asyncio
async def test_responses_parse_returns_pydantic(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
        ),
    )

    class _Responses:
        async def parse(self, **kwargs):
            assert kwargs["text_format"] is ProductHint
            assert kwargs["store"] is False
            return SimpleNamespace(
                output_parsed=ProductHint(brand="Tissot", model="PRX"),
                output_text='{"brand":"Tissot","model":"PRX"}',
                status="completed",
                refusal=None,
                output=[],
            )

    gateway = ResponsesGateway(client=SimpleNamespace(responses=_Responses()))
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=ProductHint,
        instructions="extraia marca",
        input_items="quero um Tissot PRX",
    )
    assert isinstance(result.parsed, ProductHint)
    assert result.parsed.brand == "Tissot"
    assert result.parsed.model == "PRX"
