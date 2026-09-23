"""Renderiza TODO o texto que chega ao modelo, para comparacao byte a byte.

Guarda de persona (REGRA ZERO): testar arquivo nao basta. Na Parte 1
``openai_agent.py`` ficou textualmente quase intacto enquanto o system prompt
que ele monta encolhia 59%, porque o conteudo vinha de
``site_knowledge.build_site_knowledge_text()``. O unico teste honesto compara o
TEXTO RENDERIZADO.

PROMPT SURFACE = qualquer codigo, configuracao ou dado cujo resultado seja
incorporado ao contexto enviado ao modelo, DIRETA ou INDIRETAMENTE. Um arquivo
nao e seguro so porque nao contem a palavra "persona": se o resultado chega ao
modelo, esta coberto aqui.

Ao adicionar um bloco novo, regenere o fixture a partir do baseline protegido
(ver docstring de ``tests/test_prompt_surface_regression.py``).
"""

from __future__ import annotations

import json
from typing import Any


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _grab(out: dict[str, Any], module: Any, prefix: str, names: tuple[str, ...]) -> None:
    """Captura constantes; ausencia vira marcador explicito, nunca silencio."""
    for name in names:
        out[f"{prefix}.{name}"] = getattr(module, name, "<<AUSENTE>>")


def render_prompt_surface() -> dict[str, str]:
    """Todo bloco de texto/definicao que o modelo recebe, por nome."""
    out: dict[str, Any] = {}

    # --- instrucoes de sistema e do vendedor -------------------------------
    from app import openai_agent, sales_agent

    out["openai.SYSTEM_INSTRUCTIONS"] = openai_agent.SYSTEM_INSTRUCTIONS
    _grab(out, sales_agent, "sales", (
        "SALES_PLANNER_INSTRUCTIONS", "SALES_RESPONDER_INSTRUCTIONS",
        "SALES_CLARIFICATION_INSTRUCTIONS", "SALES_INTERPRETER_INSTRUCTIONS",
        "CHECKOUT_FLOW_INSTRUCTIONS", "OUT_OF_SCOPE_REPLY", "GREETING_REPLY",
    ))

    # --- compilador/camadas de prompt (persona runtime) --------------------
    from app import prompt_compiler, prompt_layers

    _grab(out, prompt_compiler, "prompt_compiler", ("FIXED_SAFETY_POLICY",))
    _grab(out, prompt_layers, "prompt_layers", (
        "PROMPT_LAYER_ORDER", "STYLE_VOICE_RULES",
    ))

    # --- critique / judge ---------------------------------------------------
    from app import response_critique

    _grab(out, response_critique, "response_critique", (
        "CRITIQUE_JUDGE_SYSTEM_PROMPT", "REGENERATION_CONTRACT",
    ))

    # --- interpretacao de turno --------------------------------------------
    from app import turn_understanding

    _grab(out, turn_understanding, "turn_understanding", ("TURN_UNDERSTANDING_INSTRUCTIONS",))

    # --- memoria (instrucoes que chegam ao modelo) -------------------------
    from app import memory_policy, working_memory

    _grab(out, memory_policy, "memory_policy", ("MEMORY_POLICY_PROMPT",))
    _grab(out, working_memory, "working_memory", ("WORKING_MEMORY_USAGE_POLICY",))

    # --- greeting -----------------------------------------------------------
    from app import greeting_policy

    _grab(out, greeting_policy, "greeting_policy", ("_GREETING_VARIANTS",))

    # Official institutional knowledge is included in openai.SYSTEM_INSTRUCTIONS.

    # --- catalogo de capacidades (texto injetado no prompt) ----------------
    from app.capability_catalog import (
        build_capability_catalog,
        format_capability_catalog_for_prompt,
    )

    out["capability_catalog.for_prompt"] = format_capability_catalog_for_prompt()
    out["capability_catalog.payload"] = _dumps(build_capability_catalog())

    # --- instrucoes multimodais que chegam ao modelo -----------------------
    from app import image_product_id, product_image_index, story_visual_analyzer

    _grab(out, image_product_id, "image_product_id", ("IMAGE_IDENTIFY_INSTRUCTIONS",))
    _grab(out, product_image_index, "product_image_index", ("VISUAL_FINGERPRINT_INSTRUCTIONS",))
    _grab(out, story_visual_analyzer, "story_visual_analyzer", ("STORY_VISION_INSTRUCTIONS",))

    # --- copy deterministica de canal --------------------------------------
    from app import brevo_instagram_media

    _grab(out, brevo_instagram_media, "brevo_instagram_media", (
        "UNVIEWABLE_MEDIA_GUIDE_REPLY", "PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY",
    ))

    # --- definicoes de tool enviadas ao modelo -----------------------------
    try:
        from app.commerce.tools import TOOL_SCHEMAS
    except ImportError:  # baseline anterior a fronteira comercial
        from app.tray_tools import TOOL_SCHEMAS
    out["TOOL_SCHEMAS"] = _dumps(TOOL_SCHEMAS)

    return {key: (value if isinstance(value, str) else _dumps(value)) for key, value in out.items()}


def serialize(surface: dict[str, str]) -> str:
    return "".join(f"===== {key} =====\n{surface[key]}\n" for key in sorted(surface))
