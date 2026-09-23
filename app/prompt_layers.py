"""Prompt layer order and shared technical channel rules (Etapa 7).

Authority:
- Facts / safety → FIXED_SAFETY_POLICY (prompt_compiler) + FACTS at reply time
- Persona / tone / style / voice → published persona (never code)
- Channel format → channel_overlay + STYLE_VOICE_RULES (technical rules only)
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

# Technical rules shared by every channel hint. Voice and commercial style
# (openers, CTAs, how many questions, how to sell) belong to the persona.
STYLE_VOICE_RULES = "Preserve URLs completas."
