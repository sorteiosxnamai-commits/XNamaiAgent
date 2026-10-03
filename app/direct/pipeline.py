"""Reuse transport and persistence, bypass legacy semantic decisions."""
import asyncio

from app.config import get_settings
from app.models import AgentResult
from app.observability import log_event
from app.direct.agent import DirectOpenAIAgent
from app.direct.context import knowledge_documents, load_history, load_persona
from app.direct.media import input_content
from app.direct.tools import DirectTools


async def run_direct_turn(incoming, *, history=None, previous=None):
    from app.commerce.provider import get_commerce_provider
    from app.openai_client import get_async_openai_client
    from app.turn_cache import begin_turn_cache, end_turn_cache
    settings = get_settings()
    cache = begin_turn_cache()
    try:
        async with asyncio.timeout(settings.direct_timeout_seconds):
            workspace, persona = await asyncio.to_thread(load_persona, settings, incoming.workspace_id)
            incoming.workspace_id = workspace
            if history is None:
                history, previous = await asyncio.to_thread(load_history, incoming, workspace)
            tools = DirectTools(provider=get_commerce_provider(), incoming=incoming,
                                documents=knowledge_documents(persona))
            tools.products = list((previous or {}).get("direct_products") or [])[-60:]
            result = await DirectOpenAIAgent(get_async_openai_client(), settings).run_turn(
                incoming=incoming, workspace_id=workspace, history=history,
                previous=previous or {}, tools=tools, content=await input_content(incoming), persona=persona)
            result.response_metadata["persona_runtime"] = {
                "workspace_id": workspace, "persona_version_id": persona.id}
            log_event("direct.turn.completed", {k: v for k, v in result.response_metadata["direct_agent"].items()
                if k in {"model", "calls", "tools", "input_tokens", "output_tokens", "latency_ms", "prompt_version"}})
            return result
    finally:
        end_turn_cache(cache)


async def process_direct_message(incoming, customer_context):
    try:
        return await run_direct_turn(incoming)
    except Exception as exc:
        log_event("direct.turn.failed", {"error_type": type(exc).__name__})
        return AgentResult(reply_text="Não consegui concluir sua consulta agora. Pode tentar novamente em instantes?",
            safety_reason="direct_unavailable",
            response_metadata={"engine": "direct", "response_source": "direct_unavailable"})
