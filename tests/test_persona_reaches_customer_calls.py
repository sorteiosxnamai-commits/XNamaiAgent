"""Toda chamada LLM que escreve para o cliente recebe a persona publicada.

A persona vive fora do Python (persona publicada no banco / GPT). O codigo so
a INJETA por uma fonte unica: ``prompt_compiler.resolve_system_instructions``.

Duas guardas:
1. Estrutural — cada chamada LLM do runtime declara ``call_type`` literal e
   esse tipo esta classificado aqui como voltado ao cliente ou tecnico. Tipo
   novo sem classificacao FALHA: ninguem abre um caminho de resposta sem
   decidir se ele precisa de persona. Chamadas voltadas ao cliente precisam
   passar por ``resolve_system_instructions`` na mesma funcao.
2. Comportamental — esclarecimento e regeneracao do critico (os dois gaps
   encontrados) de fato enviam ao modelo o texto resolvido com persona.
"""

from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

LLM_ENTRYPOINTS = {
    "generate_text_output",
    "parse_structured_output",
    "run_tool_loop_output",
    "generate_text_sync",
    "execute_openai_call",
    "execute_openai_call_sync",
}
#: O modulo que implementa o transporte nao e call site de negocio.
TRANSPORT_MODULES = {"app/openai_gateway.py", "app/openai_runtime.py"}

CUSTOMER_FACING_CALL_TYPES = {
    "response_composition",
    "response_composition_envelope",
    "clarification",
    "tool_loop",
    "legacy",
}
#: Classificacao, extracao, selecao, juiz, visao, embeddings e audio: saida
#: estruturada ou tecnica, nunca texto enviado ao cliente. Sem persona.
TECHNICAL_CALL_TYPES = {
    "decision",
    "product_selection",
    "checkout_repair",
    "judge",
    "image_product_identify",
    "visual_fingerprint",
    "visual_embedding",
    "story_visual_analysis",
    "audio_transcription",
    # TTS le em voz alta um texto JA composto com persona; nao gera texto.
    "audio_tts",
}


def _llm_call_sites() -> list[tuple[str, str, int, str | None, bool]]:
    """(arquivo, funcao, linha, call_type, funcao chama resolve_system_instructions)."""
    sites = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "__pycache__" in path.parts or rel in TRANSPORT_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [node for node in ast.walk(fn) if isinstance(node, ast.Call)]
            names = {
                (c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", ""))
                for c in calls
            }
            resolves_persona = "resolve_system_instructions" in names
            for call in calls:
                name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
                if name not in LLM_ENTRYPOINTS:
                    continue
                call_type = None
                for keyword in call.keywords:
                    if keyword.arg == "call_type" and isinstance(keyword.value, ast.Constant):
                        call_type = str(keyword.value.value)
                sites.append((rel, fn.name, call.lineno, call_type, resolves_persona))
    return sites


SITES = _llm_call_sites()


def test_call_site_scan_is_not_empty():
    assert len(SITES) >= 15
    assert {site[3] for site in SITES} >= {"clarification", "response_composition", "decision"}


def test_every_llm_call_declares_a_classified_call_type():
    unclassified = [
        f"{rel}:{line} {fn} call_type={call_type!r}"
        for rel, fn, line, call_type, _ in SITES
        if call_type not in CUSTOMER_FACING_CALL_TYPES | TECHNICAL_CALL_TYPES
    ]
    assert not unclassified, (
        "chamada LLM sem classificacao cliente/tecnica — decida se precisa de persona:\n"
        + "\n".join(unclassified)
    )


def test_customer_facing_calls_resolve_the_published_persona():
    missing = [
        f"{rel}:{line} {fn} ({call_type})"
        for rel, fn, line, call_type, resolves in SITES
        if call_type in CUSTOMER_FACING_CALL_TYPES and not resolves
    ]
    assert not missing, "resposta ao cliente sem persona:\n" + "\n".join(missing)


PERSONA_MARKER = "<<PERSONA_PUBLICADA>>"


@pytest.fixture
def persona_spy(monkeypatch):
    """Substitui a fonte unica de persona por um espiao com marcador."""
    import app.prompt_compiler as compiler

    seen: list[str] = []

    def fake_resolve(*, fallback_instructions, **_kwargs):
        seen.append(fallback_instructions)
        return f"{PERSONA_MARKER}\n{fallback_instructions}"

    monkeypatch.setattr(compiler, "resolve_system_instructions", fake_resolve)
    return seen


def _capture_text_output(monkeypatch, reply: str) -> list[list[dict]]:
    captured: list[list[dict]] = []

    async def fake_generate_text_output(*, messages, **_kwargs):
        captured.append(messages)
        return SimpleNamespace(text=reply)

    monkeypatch.setattr("app.openai_gateway.generate_text_output", fake_generate_text_output)
    return captured


def _clarifying_interpretation():
    from app.models import SalesInterpretation

    interpretation = SalesInterpretation(
        domain="commerce",
        goal="discover",
        subject={"product_type": "capa"},
        preferences={},
        references_previous_context=False,
        needs_clarification=True,
        clarification_question="Qual é o modelo do seu celular?",
        confidence=0.9,
    )
    interpretation._source = "openai"
    return interpretation


@pytest.mark.asyncio
async def test_clarification_goes_through_persona_when_persona_is_enabled(monkeypatch, persona_spy):
    import app.sales_agent as sales_agent
    from app.models import IncomingMessage

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="m", agent_db_persona_enabled=True),
    )
    captured = _capture_text_output(monkeypatch, "Me conta o modelo do aparelho?")

    result = await sales_agent.generate_clarification_reply(
        message=IncomingMessage(channel="instagram", text="quero uma capa"),
        interpretation=_clarifying_interpretation(),
        discovery_state={"force_retrieval": False},
    )

    assert result.reply_text == "Me conta o modelo do aparelho?"
    system = captured[0][0]
    assert system["role"] == "system" and system["content"].startswith(PERSONA_MARKER)
    assert "Canal: instagram" in persona_spy[0]
    payload = json.loads(captured[0][-1]["content"])
    assert payload["suggested_question"] == "Qual é o modelo do seu celular?"


@pytest.mark.asyncio
async def test_clarification_without_persona_keeps_the_single_call_path(monkeypatch, persona_spy):
    """Sem persona publicada nao ha voz a aplicar: nenhuma chamada extra."""
    import app.sales_agent as sales_agent
    from app.models import IncomingMessage

    monkeypatch.setattr(
        sales_agent,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="m", agent_db_persona_enabled=False),
    )
    captured = _capture_text_output(monkeypatch, "nao deveria ser chamado")

    result = await sales_agent.generate_clarification_reply(
        message=IncomingMessage(channel="whatsapp", text="quero uma capa"),
        interpretation=_clarifying_interpretation(),
        discovery_state={"force_retrieval": False},
    )

    assert result.reply_text == "Qual é o modelo do seu celular?"
    assert captured == []


@pytest.mark.asyncio
async def test_critique_regeneration_goes_through_persona(monkeypatch, persona_spy):
    import app.response_critique as critique
    from app.models import AgentResult, IncomingMessage

    monkeypatch.setattr(
        critique,
        "get_settings",
        lambda: SimpleNamespace(openai_api_key="sk-test", openai_model="m"),
    )
    captured = _capture_text_output(monkeypatch, "Resposta regenerada.")

    regenerated = await critique._regenerate_reply(
        incoming=IncomingMessage(channel="instagram", text="tem fone bluetooth?"),
        result=AgentResult(reply_text="resposta anterior", intent="commerce"),
        verdict=critique.CritiqueVerdict(pass_check=False, issues=["x"]),
        api_facts={},
        recent_turns=[],
        commerce_state=None,
    )

    assert regenerated is not None
    assert regenerated.reply_text == "Resposta regenerada."
    assert captured[0][0]["content"].startswith(PERSONA_MARKER)
    contract = persona_spy[0]
    assert critique.REGENERATION_CONTRACT in contract
    assert "Canal: instagram" in contract
    # A regeneracao nao carrega voz propria nem canal fixo.
    assert "WhatsApp" not in critique.REGENERATION_CONTRACT
    assert "Você é o agente" not in critique.REGENERATION_CONTRACT
