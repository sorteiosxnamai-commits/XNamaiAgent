from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.openai_gateway import (
    ChatCompletionsGateway,
    ResponsesGateway,
    ShadowOpenAIGateway,
)


class _Out(BaseModel):
    n: int


@pytest.mark.asyncio
async def test_shadow_does_not_duplicate_mutation_side_effects(monkeypatch):
    """Shadow must never invoke write tools; structured path has no writes."""
    writes: list[str] = []

    monkeypatch.setattr(
        "app.openai_gateway.get_settings",
        lambda: SimpleNamespace(
            openai_shadow_sample_rate=1.0,
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
                            parsed=_Out(n=1),
                            content='{"n":1}',
                            refusal=None,
                        )
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    class _Resp:
        async def parse(self, **kwargs):
            writes.append("responses")
            return SimpleNamespace(
                output_parsed=_Out(n=1),
                output_text='{"n":1}',
                status="completed",
                refusal=None,
                output=[],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Chat()),
        responses=_Resp(),
    )
    gateway = ShadowOpenAIGateway(
        primary=ChatCompletionsGateway(client=client),
        shadow=ResponsesGateway(client=client),
    )
    result = await gateway.parse_structured(
        model="gpt-4.1-mini",
        text_format=_Out,
        messages=[{"role": "user", "content": "x"}],
    )
    assert result.parsed.n == 1
    assert writes == ["chat", "responses"]
    assert result.api_mode == "shadow"


def test_audio_and_embeddings_modules_still_importable():
    # Phase 1 must not migrate audio/embeddings onto Responses.
    from app import audio_service, product_image_index

    assert hasattr(audio_service, "transcribe_audio_url")
    assert hasattr(product_image_index, "visual_search_from_caption")
