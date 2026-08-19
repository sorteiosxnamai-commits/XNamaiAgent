"""Phase 8: Chat Completions demoted to rollback/fallback only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.openai_errors import OpenAIGatewayError
from app.openai_gateway import (
    ChatCompletionsGateway,
    FallbackOpenAIGateway,
    ResponsesGateway,
    build_openai_gateway,
    generate_text_sync,
    reset_openai_gateway,
)


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class _Out(BaseModel):
    n: int


@pytest.fixture(autouse=True)
def _reset_gateway():
    reset_openai_gateway()
    yield
    reset_openai_gateway()


def test_chat_primary_disabled_redirects_to_responses_fallback(monkeypatch):
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_api_mode="chat_completions",
            openai_chat_completions_primary_allowed=False,
            openai_responses_fallback_to_chat=True,
        ),
    )
    gateway = build_openai_gateway()
    assert isinstance(gateway, FallbackOpenAIGateway)


def test_business_modules_do_not_import_chat_completions_gateway():
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if path.name in {
            "openai_gateway.py",
            "openai_client.py",
            "openai_routing.py",
            "openai_errors.py",
            "openai_runtime.py",
        }:
            continue
        text = path.read_text(encoding="utf-8")
        if "ChatCompletionsGateway" in text or "chat.completions." in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


@pytest.mark.asyncio
async def test_fallback_gateway_uses_chat_on_responses_failure(monkeypatch):
    writes: list[str] = []
    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_responses_fallback_to_chat=True,
            openai_store_responses=False,
            openai_responses_structured_enabled=True,
        ),
    )

    class _Chat:
        async def parse(self, **kwargs):
            writes.append("chat")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            parsed=_Out(n=7),
                            content='{"n":7}',
                            refusal=None,
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    class _Resp:
        async def parse(self, **kwargs):
            writes.append("responses")
            raise OpenAIGatewayError("down", code="responses_down")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Chat()),
        responses=_Resp(),
    )
    gateway = FallbackOpenAIGateway(
        primary=ResponsesGateway(client=client),
        fallback=ChatCompletionsGateway(client=client),
    )
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_Out,
        messages=[{"role": "user", "content": "x"}],
    )
    assert writes == ["responses", "chat"]
    assert result.parsed.n == 7
    assert result.api_mode == "responses_fallback_chat"


def test_generate_text_sync_uses_async_gateway(monkeypatch):
    async def _fake_generate(**kwargs):
        return SimpleNamespace(
            text="via-gateway",
            raw_response=None,
            api_mode="responses",
            model=kwargs.get("model"),
            latency_ms=1.0,
        )

    monkeypatch.setattr(
        "app.openai_gateway.generate_text_output",
        _fake_generate,
    )
    result = generate_text_sync(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "oi"}],
    )
    assert result.text == "via-gateway"
    assert result.api_mode == "responses"
