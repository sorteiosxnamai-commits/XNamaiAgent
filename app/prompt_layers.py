"""Prompt layer order and shared style voice (Etapa 7).

Authority:
- Facts / safety → FIXED_SAFETY_POLICY (prompt_compiler) + FACTS at reply time
- Persona / tone identity → DB persona
- Channel format → channel_overlay + STYLE_VOICE_RULES (this module)
- Presentation → thin presenter (URL / blocks / similar); not a second style editor

Do not restate STYLE_VOICE_RULES in sales_agent or presenter regex when thin mode is on.
"""

from __future__ import annotations

# Documented compile order used by prompt_compiler.resolve_system_instructions.
# PROMPT_LAYER_ORDER documented compile stack (Etapa 7–8).
PROMPT_LAYER_ORDER: tuple[str, ...] = (
    "fixed_safety_policy",
    "user_managed_persona",
    "approved_instruction_extensions",
    "channel_overlay",
    "customer_memory",
    "conversation_summary",
    "operational_contract",
)

# Single style/voice contract for channel hints and responder grounding.
STYLE_VOICE_RULES = (
    "Responda primeiro à pergunta; evite aberturas genéricas "
    "(Claro/Com certeza); no máximo uma pergunta principal; "
    "preserve URLs completas; sem forçar venda em suporte."
)

# Short block appended to sales responder when presenter is thin/shadow
# (style lives in prompt, not post-hoc regex surgery).
RESPONDER_STYLE_GROUNDING = (
    "Estilo: responda primeiro ao pedido; sem aberturas genéricas "
    "(Claro/Com certeza); no máximo uma pergunta principal por mensagem "
    "comercial; preserve URLs completas; no máximo um CTA."
)
