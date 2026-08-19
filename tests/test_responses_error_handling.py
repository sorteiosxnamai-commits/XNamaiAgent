from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.openai_errors import (
    OpenAIEmptyOutputError,
    OpenAIIncompleteError,
    OpenAIRefusalError,
    OpenAISchemaError,
    OpenAITimeoutGatewayError,
)
from app.openai_gateway import ResponsesGateway


class _Out(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_incomplete_status_raises(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
        ),
    )

    class _Resp:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                status="incomplete",
                output_parsed=None,
                output_text="",
                refusal=None,
                output=[],
            )

    gateway = ResponsesGateway(client=SimpleNamespace(responses=_Resp()))
    with pytest.raises(OpenAIIncompleteError):
        await gateway.parse_structured(
            model="gpt-4.1-mini",
            text_format=_Out,
            instructions="x",
            input_items="y",
        )


@pytest.mark.asyncio
async def test_timeout_mapped(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
        ),
    )

    class _Resp:
        async def parse(self, **kwargs):
            raise TimeoutError("slow")

    gateway = ResponsesGateway(client=SimpleNamespace(responses=_Resp()))
    with pytest.raises(OpenAITimeoutGatewayError):
        await gateway.parse_structured(
            model="gpt-4.1-mini",
            text_format=_Out,
            instructions="x",
            input_items="y",
        )


@pytest.mark.asyncio
async def test_empty_text_raises(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(openai_store_responses=False),
    )

    class _Resp:
        async def create(self, **kwargs):
            return SimpleNamespace(
                status="completed",
                output_text="",
                output=[],
                refusal=None,
            )

    gateway = ResponsesGateway(client=SimpleNamespace(responses=_Resp()))
    with pytest.raises(OpenAIEmptyOutputError):
        await gateway.generate_text(
            model="gpt-4.1-mini",
            instructions="x",
            input_items="y",
        )


@pytest.mark.asyncio
async def test_refusal_and_schema(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
        ),
    )

    class _Refuse:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                status="completed",
                output_parsed=None,
                output_text="",
                refusal="blocked",
                output=[],
            )

    with pytest.raises(OpenAIRefusalError):
        await ResponsesGateway(client=SimpleNamespace(responses=_Refuse())).parse_structured(
            model="gpt-4.1-mini",
            text_format=_Out,
            instructions="x",
            input_items="y",
        )

    class _NoParsed:
        async def parse(self, **kwargs):
            return SimpleNamespace(
                status="completed",
                output_parsed=None,
                output_text="",
                refusal=None,
                output=[],
            )

    with pytest.raises(OpenAISchemaError):
        await ResponsesGateway(
            client=SimpleNamespace(responses=_NoParsed())
        ).parse_structured(
            model="gpt-4.1-mini",
            text_format=_Out,
            instructions="x",
            input_items="y",
        )
