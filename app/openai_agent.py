from __future__ import annotations

import re
import json

from openai import APIError
from .agent_replies import (
    build_available_numbers_reply,
    build_balance_reply,
    build_coupon_code_reply,
    build_current_raffle_reply,
    build_raffle_history_reply,
    build_rules_reply_result,
    build_simulation_reply,
    build_preferred_name_reply,
    _third_party_reply,
)
from .config import get_settings
from .commerce_context import CommerceConversationState, apply_commerce_domain_context
from .db import load_recent_conversation_turns
from .image_product_id import handle_image_product_search, image_search_eligible
from .context_builder import (
    build_template_fallback,
    detect_primary_intent,
    format_facts_for_prompt,
    gather_customer_facts,
)
from .guardrails import (
    detect_available_numbers_inquiry,
    detect_blocked_request,
    default_safe_handoff,
)
from .handoff_service import build_human_handoff_result, should_request_human_handoff
from .models import IncomingMessage, AgentResult
from .turn_runtime import LLMCallBudgetExceeded
from .context_resume import (
    build_contextual_greeting,
    has_resumable_commerce,
    is_payment_link_request,
    is_soft_greeting,
    is_unpaid_order_resume_request,
    should_resume_pending_order,
)
from .order_context_recovery import (
    extract_handles_from_conversation,
    hydrate_state_from_handles,
    recover_order_id_from_customer,
)
from .order_service import (
    contains_tax_document_candidate,
    extract_order_reference,
    extract_valid_tax_document,
    find_order_by_customer_document,
    get_order_facts,
    invalid_tax_document_result,
    is_order_lookup_request,
)
from .payment_service import inspect_order_payment
from .repository import detect_third_party_account_inquiry, find_coupon_balance_by_phone
from .site_knowledge import HUMAN_SUPPORT_MESSAGE, build_site_knowledge_text, NS_SALES_WHATSAPP
from .vip_profiles import build_vip_openai_context, get_vip_profile, pick_vip_nickname
from .user_preferences import detect_preferred_name_update
from .tray_tools import TOOL_SCHEMAS, execute_tool
from .sales_agent import (
    GREETING_REPLY,
    OUT_OF_SCOPE_REPLY,
    deterministic_scope,
    handle_sales_message,
    interpret_message,
    _is_greeting,
)
from .greeting_policy import choose_greeting_reply


SYSTEM_INSTRUCTIONS = f"""
Você é o NewStoreAgent, atendente virtual da New Store Sorteios.

{build_site_knowledge_text()}

Regras obrigatórias:
- Responda em português do Brasil, de forma curta e clara para WhatsApp.
- Use APENAS os dados consultados no banco e a base oficial acima.
- Nunca invente saldo, cupom, números ou resultados.
- Responda primeiro o que o cliente perguntou; só depois complemente se fizer sentido.
- Nunca consulte ou revele dados de outra pessoa.
- Se o cliente não tiver telefone cadastrado, oriente a acessar https://www.sorteionewstore.com.br/ e incluir o telefone no perfil.
- Não altere cadastro ou participações pelo WhatsApp. Em compras, execute somente
  capacidades comerciais validadas e nunca colete dados sensíveis de pagamento no chat.
- Não prometa ganhar sorteio; explique regras oficiais.
- Se não souber, oriente o site ou encaminhe para a equipe no WhatsApp {NS_SALES_WHATSAPP}.
- Use a memória do cliente quando disponível; não repita perguntas sobre nome ou preferências já registradas.
- Adapte tom e tamanho da resposta ao estilo preferido do cliente.
- Se a mensagem veio de áudio transcrito, responda naturalmente ao conteúdo falado.
- Para produtos, pre\u00e7os, estoque, clientes e cupons, use as ferramentas de consulta quando dispon\u00edveis.
- Nunca invente pre\u00e7o, estoque, parcelamento ou validade de cupom. `promotional_price` nulo n\u00e3o \u00e9 promo\u00e7\u00e3o.
- Para estoque, considere todos os campos retornados, n\u00e3o apenas `stock > 0`.
- O banco local \u00e9 a fonte oficial para saldo, Cart\u00e3o Presente pessoal, sorteios, participa\u00e7\u00f5es, n\u00fameros e hist\u00f3rico.
- O TrayAdapter \u00e9 a fonte oficial para cat\u00e1logo, produtos, marcas, pre\u00e7os, estoque, EAN, refer\u00eancia e condi\u00e7\u00f5es comerciais.
- Para qualquer informa\u00e7\u00e3o comercial atual, use as tools do TrayAdapter; nunca use exemplos do site como pre\u00e7o ou estoque atual.
- Responda somente sobre a NewStore, seus produtos, compras, atendimento comercial e sorteios; para assuntos externos, use a recusa curta de escopo.
""".strip()

STORE_LOOKUP_UNAVAILABLE = "N\u00e3o consegui consultar as informa\u00e7\u00f5es da loja neste momento. Tente novamente em instantes."
GENERAL_GREETING_FALLBACK = "Ol\u00e1! Como posso ajudar?"
STORE_KNOWLEDGE_UNAVAILABLE = "Ainda não tenho essa informação oficial da loja disponível neste atendimento."


def _annotate_agent_result(result: AgentResult, **metadata: object) -> AgentResult:
    for key, value in metadata.items():
        if value is not None and key not in result.response_metadata:
            result.response_metadata[key] = value
    # Phase 8: count skipped LLM slots when deterministic / partial paths win.
    if "used_openai_interpreter" in metadata or "used_openai_responder" in metadata:
        from .runtime_context import register_avoided_llm_call

        used_interpreter = bool(
            result.response_metadata.get("used_openai_interpreter")
        )
        used_responder = bool(result.response_metadata.get("used_openai_responder"))
        source = str(
            result.response_metadata.get("response_source") or "deterministic"
        )
        skipped: list[str] = []
        if not used_interpreter:
            skipped.append("decision")
        if not used_responder:
            skipped.append("response_composition")
        if skipped:
            register_avoided_llm_call(
                f"path:{source}",
                intended_call_types=skipped,
            )
    return result


def _preferred_name_reply_if_requested(message: IncomingMessage, facts: dict) -> AgentResult | None:
    if not detect_preferred_name_update(message.text):
        return None
    account = facts.get("account") or {}
    if not account.get("found"):
        account = find_coupon_balance_by_phone(message.sender_phone, message.text)
    return build_preferred_name_reply(message, account)


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _sanitize_log_message(text: str) -> str:
    redacted = re.sub(r"sk-(?:proj-)?[^\s'\"]+", "sk-***", text or "")
    return redacted[:300]


def _non_handoff_fallback(message: IncomingMessage, facts: dict) -> str:
    fallback = build_template_fallback(message, facts)
    if fallback:
        return fallback
    if facts.get("primary_intent") == "commerce":
        return STORE_LOOKUP_UNAVAILABLE
    if facts.get("primary_intent") == "general":
        if facts.get("scope_domain") == "store_general":
            return STORE_KNOWLEDGE_UNAVAILABLE
        return GENERAL_GREETING_FALLBACK
    return "N\u00e3o consegui concluir a consulta neste momento. Tente novamente em instantes."


def _is_personal_intent(intent: str) -> bool:
    return intent in {"balance", "coupon_code", "raffle_history", "simulation"}


def _third_party_guardrail(message: IncomingMessage, primary_intent: str) -> AgentResult | None:
    if _is_personal_intent(primary_intent) and detect_third_party_account_inquiry(message.text, message.sender_phone):
        return _third_party_reply()
    return None


def _local_raffle_reply(message: IncomingMessage, facts: dict) -> AgentResult | None:
    handlers = {
        "balance": build_balance_reply,
        "coupon_code": build_coupon_code_reply,
        "simulation": build_simulation_reply,
        "raffle_history": build_raffle_history_reply,
        "current_raffle": build_current_raffle_reply,
        "rules": build_rules_reply_result,
    }
    handler = handlers.get(str(facts.get("primary_intent")))
    if handler:
        print("[raffle.route]", {"intent": facts.get("primary_intent")})
    return handler(message) if handler else None


def build_agent_input(message: IncomingMessage, customer_context: dict, facts: dict) -> str:
    from .working_memory import format_working_memory_block

    vip_block = ""
    vip = get_vip_profile(message.sender_phone)
    if vip:
        nickname = pick_vip_nickname(vip, message.text)
        vip_block = f"\n\n{build_vip_openai_context(vip, nickname)}\n"

    display_name = facts.get("display_name") or customer_context.get("display_name")
    display_label = display_name or message.sender_name or "não informado"
    modality_note = ""
    if message.input_modality == "audio":
        modality_note = "\n- Origem: áudio transcrito para texto"

    channel_label = {
        "instagram": "Instagram",
        "facebook": "Facebook",
        "whatsapp": "WhatsApp",
        "widget": "chat do site",
    }.get(message.channel, message.channel or "canal não identificado")
    working_memory_block = format_working_memory_block(
        customer_context.get("_commerce_state")
    )
    memory_section = f"\n\n{working_memory_block}" if working_memory_block else ""
    return f"""
Mensagem recebida via {channel_label}:
- Nome para tratamento: {display_label}
- Telefone presente: {'sim' if message.sender_phone else 'não'}{modality_note}
- Texto do cliente: {message.text}
- Intenção detectada: {facts.get('primary_intent')}
{memory_section}

{format_facts_for_prompt(facts)}
{vip_block}
Responda de forma natural, objetiva e correta.
Use WORKING_MEMORY só internamente; não ofereça pedido/link/dados sem o cliente pedir.
""".strip()


def generate_openai_reply(
    message: IncomingMessage,
    customer_context: dict,
    facts: dict,
) -> AgentResult:
    settings = get_settings()
    if not settings.openai_api_key:
        return AgentResult(
            reply_text=_non_handoff_fallback(message, facts),
            intent=str(facts.get("primary_intent") or "general_support"),
            handoff_required=False,
            safety_reason="openai_api_key_missing",
        )

    from .prompt_compiler import legacy_contract_extra_blocks, resolve_system_instructions

    user_input = build_agent_input(message, customer_context, facts)
    system_instructions = resolve_system_instructions(
        fallback_instructions=SYSTEM_INSTRUCTIONS,
        incoming=message,
        extra_system_blocks=legacy_contract_extra_blocks(
            SYSTEM_INSTRUCTIONS,
            tag="legacy_agent_contract",
        ),
    )
    legacy_messages = [
        {"role": "system", "content": system_instructions},
        {"role": "user", "content": user_input},
    ]
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import generate_text_sync

        text_result = generate_text_sync(
            model=settings.openai_model,
            messages=legacy_messages,
            temperature=0.3,
            call_type="legacy",
        )
        content = text_result.text
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded) as exc:
        status_code = getattr(exc, "status_code", None)
        print("[openai.agent] request_failed", {
            "status_code": status_code,
            "error_type": type(exc).__name__,
            "model": settings.openai_model,
            "message": _sanitize_log_message(str(exc)),
        })
        return AgentResult(
            reply_text=_non_handoff_fallback(message, facts),
            intent=str(facts.get("primary_intent") or "general_support"),
            handoff_required=False,
            safety_reason=f"openai_error_{status_code or type(exc).__name__}",
        )

    reply = _truncate(
        content or _non_handoff_fallback(message, facts),
        settings.max_reply_chars,
    )
    return AgentResult(
        reply_text=reply,
        intent=str(facts.get("primary_intent") or "general_support"),
        handoff_required=False,
    )


def generate_agent_reply(message: IncomingMessage, customer_context: dict) -> AgentResult:
    blocked_reason = detect_blocked_request(message.text)
    if blocked_reason:
        return AgentResult(
            reply_text=default_safe_handoff(),
            intent="handoff",
            handoff_required=True,
            safety_reason=blocked_reason,
        )

    scope = deterministic_scope(message.text)
    print("[agent.scope]", {"domain": scope.get("domain")})
    if scope.get("domain") == "out_of_scope":
        return AgentResult(reply_text=OUT_OF_SCOPE_REPLY, intent="out_of_scope", handoff_required=False, safety_reason="scope_refusal")
    if scope.get("domain") == "greeting":
        return AgentResult(
            reply_text=choose_greeting_reply(None),
            intent="general",
            handoff_required=False,
        )
    primary_intent = detect_primary_intent(message.text)
    print("[agent.route]", {"inbound_id": (message.raw or {}).get("inbound_id"), "primary_intent": primary_intent})
    third_party_reply = _third_party_guardrail(message, primary_intent)
    if third_party_reply:
        return third_party_reply

    if message.input_modality == "audio" and message.transcription_failed:
        return AgentResult(
            reply_text=(
                "Recebi seu áudio, mas não consegui entender agora. "
                "Pode repetir por texto ou enviar outro áudio?"
            ),
            intent="audio_transcription_failed",
            handoff_required=False,
        )

    if message.input_modality == "audio" and not (message.text or "").strip():
        return AgentResult(
            reply_text=(
                "Recebi seu áudio, mas não consegui transcrever. "
                "Pode repetir por texto ou enviar outro áudio?"
            ),
            intent="audio_transcription_failed",
            handoff_required=False,
        )

    facts = gather_customer_facts(message, customer_context)
    facts["scope_domain"] = scope.get("domain")
    preferred_reply = _preferred_name_reply_if_requested(message, facts)
    if preferred_reply:
        return preferred_reply
    local_reply = _local_raffle_reply(message, facts)
    if local_reply:
        return local_reply
    if detect_available_numbers_inquiry(message.text):
        return build_available_numbers_reply(message)
    print("[openai.agent] routing", {
        "mode": "openai_with_db_context",
        "primary_intent": facts.get("primary_intent"),
        "input_modality": message.input_modality,
        "text_length": len(message.text or ""),
        "has_openai_key": bool(get_settings().openai_api_key),
        "transcription_failed": message.transcription_failed,
    })
    return generate_openai_reply(message, customer_context, facts)


async def generate_openai_reply_async(message: IncomingMessage, customer_context: dict, facts: dict) -> AgentResult:
    settings = get_settings()
    if not settings.openai_api_key:
        return generate_openai_reply(message, customer_context, facts)

    from .prompt_compiler import legacy_contract_extra_blocks, resolve_system_instructions

    system_instructions = resolve_system_instructions(
        fallback_instructions=SYSTEM_INSTRUCTIONS,
        incoming=message,
        extra_system_blocks=legacy_contract_extra_blocks(
            SYSTEM_INSTRUCTIONS,
            tag="legacy_agent_contract",
        ),
    )
    messages: list[dict] = [
        {"role": "system", "content": system_instructions},
        {"role": "user", "content": build_agent_input(message, customer_context, facts)},
    ]
    tools = (
        TOOL_SCHEMAS
        if facts.get("primary_intent") == "commerce"
        and settings.tray_adapter_url
        and settings.tray_adapter_token
        else None
    )
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import generate_text_output, run_tool_loop_output

        if not tools:
            text_result = await generate_text_output(
                model=settings.openai_model,
                messages=messages,
                temperature=0.3,
                call_type="response_composition",
            )
            reply = _truncate(
                text_result.text or _non_handoff_fallback(message, facts),
                settings.max_reply_chars,
            )
            return AgentResult(
                reply_text=reply,
                intent=str(facts.get("primary_intent") or "general_support"),
            )

        async def _execute_allowed(name: str, arguments: dict) -> dict:
            result = await execute_tool(name, arguments)
            return result

        loop_result = await run_tool_loop_output(
            model=settings.openai_model,
            tools=tools,
            execute_tool=_execute_allowed,
            messages=messages,
            temperature=0.3,
            parallel_tool_calls=True,
            max_rounds=3,
            call_type="tool_loop",
        )
        for item in loop_result.tool_results:
            if isinstance(item.get("result"), dict) and "error" in item["result"]:
                return AgentResult(
                    reply_text=_non_handoff_fallback(message, facts),
                    intent=str(facts.get("primary_intent") or "store_lookup"),
                    handoff_required=False,
                    safety_reason="tray_adapter_unavailable",
                )
        if loop_result.limit_reached and not loop_result.text:
            return AgentResult(
                reply_text=_non_handoff_fallback(message, facts),
                intent=str(facts.get("primary_intent") or "store_lookup"),
                handoff_required=False,
                safety_reason="tool_loop_limit",
            )
        reply = _truncate(
            loop_result.text or _non_handoff_fallback(message, facts),
            settings.max_reply_chars,
        )
        return AgentResult(
            reply_text=reply,
            intent=str(facts.get("primary_intent") or "general_support"),
        )
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print("[openai.agent] tools_request_failed", {"error_type": type(exc).__name__, "message": _sanitize_log_message(str(exc))})
        return AgentResult(reply_text=_non_handoff_fallback(message, facts), intent=str(facts.get("primary_intent") or "store_lookup"), handoff_required=False, safety_reason="tools_request_failed")


async def generate_agent_reply_async(message: IncomingMessage, customer_context: dict) -> AgentResult:
    blocked_reason = detect_blocked_request(message.text)
    if blocked_reason:
        return _annotate_agent_result(
            build_human_handoff_result(reason=blocked_reason),
            domain="guardrail",
            response_source="guardrail",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    human_reason = should_request_human_handoff(message)
    if human_reason:
        return _annotate_agent_result(
            build_human_handoff_result(reason=human_reason),
            domain="guardrail",
            response_source="handoff",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )

    raw_inbound_id = (message.raw or {}).get("inbound_id")
    try:
        inbound_id = int(raw_inbound_id) if raw_inbound_id is not None else None
    except (TypeError, ValueError):
        inbound_id = None
    settings = get_settings()
    from .history_window import count_user_assistant_turns, select_model_history_turns

    history_limit = int(getattr(settings, "agent_history_limit", 12))
    history_hard_cap = int(getattr(settings, "agent_history_hard_cap", 80))
    history_lookup = {
        "conversation_id": message.conversation_id,
        "sender_phone": message.sender_phone,
        "before_inbound_id": inbound_id,
        # Load operational window for deterministic recovery (orders/payment URLs).
        "limit": history_hard_cap,
        "sender_key": message.sender_key,
        "hard_cap": history_hard_cap,
    }
    recovery_turns = load_recent_conversation_turns(**history_lookup)
    model_turns = select_model_history_turns(recovery_turns, limit=history_limit)
    recent_turns = model_turns
    context_source = (
        "conversation_id"
        if message.conversation_id
        else ("sender_key" if message.sender_key else ("sender_phone" if message.sender_phone else "none"))
    )
    from .observability import (
        log_event,
        redact_text,
        summarize_commerce_state,
        summarize_history_turns,
    )

    recovery_counts = count_user_assistant_turns(recovery_turns)
    model_counts = count_user_assistant_turns(model_turns)
    log_event(
        "sales.context",
        {
            "history_turns": model_counts["total"],
            "recovery_turns": recovery_counts["total"],
            "history_limit": history_limit,
            "history_hard_cap": history_hard_cap,
            "history_user_turns": model_counts["user"],
            "history_assistant_turns": model_counts["assistant"],
            "conversation_id_present": bool(message.conversation_id),
            "sender_key_present": bool(message.sender_key),
            "before_inbound_id_present": inbound_id is not None,
            "context_source": context_source,
        },
    )
    customer_context["_conversation_turns"] = recovery_turns
    customer_context["_model_conversation_turns"] = model_turns
    commerce_state = CommerceConversationState.from_payload(
        customer_context.get("_commerce_state")
    )
    log_event(
        "history.loaded",
        {
            "context_source": context_source,
            "history_turns": model_counts["total"],
            "recovery_turns": recovery_counts["total"],
            "history_limit": history_limit,
            "history_hard_cap": history_hard_cap,
            "history_preview": summarize_history_turns(model_turns),
            "recovery_preview": summarize_history_turns(recovery_turns),
            "commerce_state": summarize_commerce_state(commerce_state),
            "inbound_text_preview": redact_text(message.text, max_chars=500),
            "channel": message.channel,
        },
    )
    if commerce_state.pending_action == "awaiting_order_customer_document":
        customer_document = extract_valid_tax_document(message.text)
        if customer_document:
            document_kind, document = customer_document
            result = await find_order_by_customer_document(
                state=commerce_state,
                execute=execute_tool,
                document_kind=document_kind,
                document=document,
            )
            return _annotate_agent_result(
                result,
                domain="commerce",
                response_source="deterministic_fallback",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=bool(result.response_metadata.get("used_tray")),
            )
        if contains_tax_document_candidate(message.text):
            result = invalid_tax_document_result()
            return _annotate_agent_result(
                result,
                domain="commerce",
                response_source="deterministic_fallback",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
            )
    order_reference = extract_order_reference(message.text)
    soft_greeting = _is_greeting(message.text) or is_soft_greeting(message.text)
    context_handles = extract_handles_from_conversation(
        state=commerce_state,
        recent_turns=recovery_turns or recent_turns,
        message_text=message.text,
    )
    commerce_state = hydrate_state_from_handles(commerce_state, context_handles)
    customer_context["_commerce_state"] = commerce_state.model_dump(mode="json")
    # Keep memory loaded on soft greetings without dumping order/payment unsolicited.
    if (
        soft_greeting
        and has_resumable_commerce(commerce_state)
        and not is_order_lookup_request(message.text)
        and not is_unpaid_order_resume_request(message.text)
        and not is_payment_link_request(message.text)
    ):
        resume = build_contextual_greeting(commerce_state)
        return _annotate_agent_result(
            resume,
            domain=resume.response_metadata.get("domain") or "greeting",
            response_source=resume.response_metadata.get(
                "response_source",
                "context_resume_soft",
            ),
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    # Fast path: customer asks for the link and we already recovered it from transcript.
    if is_payment_link_request(message.text) and commerce_state.order_payment_url:
        order_label = commerce_state.order_id or commerce_state.order_lookup_id
        reply = (
            f"Seu pedido {order_label} ainda está aguardando pagamento. "
            f"Segue o link: {commerce_state.order_payment_url}"
            if order_label
            else (
                "Seu pedido ainda está aguardando pagamento. "
                f"Segue o link: {commerce_state.order_payment_url}"
            )
        )
        print("[sales.order.route]", {
            "route": "transcript_payment_url",
            "order_id_present": bool(order_label),
            "payment_url_present": True,
        })
        return _annotate_agent_result(
            AgentResult(
                reply_text=reply,
                intent="commerce",
                commercial_data={
                    "order_id": order_label,
                    "payment": {
                        "payment_url": commerce_state.order_payment_url,
                        "status": commerce_state.order_payment_status or "awaiting_payment",
                    },
                },
                response_metadata={
                    "domain": "commerce",
                    "pending_action": "awaiting_payment",
                    "order_state": {"order_id": order_label} if order_label else {},
                    "payment_state": {
                        "order_payment_url": commerce_state.order_payment_url,
                        "order_payment_status": (
                            commerce_state.order_payment_status or "awaiting_payment"
                        ),
                    },
                    "used_tray": False,
                },
            ),
            domain="commerce",
            response_source="context_resume_payment_url",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
        )
    wants_order_context = (
        is_order_lookup_request(message.text)
        or is_payment_link_request(message.text)
        or is_unpaid_order_resume_request(message.text)
    )
    known_order_tokens = [
        token
        for token in (
            order_reference,
            commerce_state.order_id,
            commerce_state.order_lookup_id,
            *(context_handles.get("order_ids") or []),
        )
        if token
    ]
    has_numeric_order_id = any(str(token).isdigit() for token in known_order_tokens)
    # Recover when missing order context, or when we only have storefront hex codes
    # (Tray get_order*_ endpoints need the numeric internal id).
    if wants_order_context and (
        not (
            order_reference
            or commerce_state.order_id
            or commerce_state.order_lookup_id
            or commerce_state.order_session_id
            or commerce_state.cart_session_id
            or commerce_state.order_payment_url
        )
        or not has_numeric_order_id
    ):
        recovered_order_id = await recover_order_id_from_customer(
            execute=execute_tool,
            handles=context_handles,
            preferred_codes=[str(token) for token in known_order_tokens],
        )
        if recovered_order_id:
            commerce_state.order_id = recovered_order_id
            commerce_state.order_lookup_id = (
                commerce_state.order_lookup_id or recovered_order_id
            )
            if commerce_state.pending_action is None:
                commerce_state.pending_action = "awaiting_payment"
    resume_pending_order = should_resume_pending_order(
        message.text,
        commerce_state,
        is_greeting=soft_greeting,
        allow_without_state=bool(
            context_handles.get("order_ids")
            or context_handles.get("payment_urls")
            or context_handles.get("documents")
            or context_handles.get("emails")
        ),
    )
    if (
        is_order_lookup_request(message.text)
        or resume_pending_order
        or is_payment_link_request(message.text)
    ) and (
        order_reference
        or commerce_state.order_id
        or commerce_state.order_lookup_id
        or commerce_state.order_session_id
        or commerce_state.cart_session_id
        or commerce_state.order_payment_url
    ):
        print("[sales.order.route]", {
            "route": (
                "context_resume_payment"
                if (
                    resume_pending_order or is_payment_link_request(message.text)
                )
                and not is_order_lookup_request(message.text)
                else "deterministic_status_lookup"
            ),
            "order_reference_present": bool(order_reference),
            "state_order_present": bool(commerce_state.order_id),
            "resume_pending_order": resume_pending_order,
            "recovered_from_transcript": bool(context_handles.get("order_ids")),
            "customer_handles": {
                "emails": len(context_handles.get("emails") or []),
                "documents": len(context_handles.get("documents") or []),
            },
        })
        if (
            (
                resume_pending_order
                or is_payment_link_request(message.text)
            )
            and commerce_state.order_id
            and (
                commerce_state.pending_action == "awaiting_payment"
                or commerce_state.order_payment_url
                or is_payment_link_request(message.text)
                or is_unpaid_order_resume_request(message.text)
            )
            and not is_order_lookup_request(message.text)
        ):
            result = await inspect_order_payment(
                state=commerce_state,
                execute=execute_tool,
                order_id=commerce_state.order_id,
            )
            if not (result.commercial_data or {}).get("payment", {}).get("payment_url"):
                if commerce_state.order_payment_url:
                    result = AgentResult(
                        reply_text=(
                            f"Seu pedido {commerce_state.order_id} ainda está aguardando "
                            f"pagamento. Segue o link: {commerce_state.order_payment_url}"
                        ),
                        intent="commerce",
                        commercial_data={
                            "order_id": commerce_state.order_id,
                            "payment": {
                                "payment_url": commerce_state.order_payment_url,
                                "status": commerce_state.order_payment_status,
                            },
                        },
                        response_metadata={
                            "domain": "commerce",
                            "pending_action": "awaiting_payment",
                            "order_state": {"order_id": commerce_state.order_id},
                            "payment_state": {
                                "order_payment_url": commerce_state.order_payment_url,
                                "order_payment_status": (
                                    commerce_state.order_payment_status
                                ),
                            },
                        },
                    )
        else:
            result = await get_order_facts(
                state=commerce_state,
                execute=execute_tool,
                order_id=order_reference,
            )
            # Status lookup with no payment facts: still try payment when customer asks.
            if (
                is_unpaid_order_resume_request(message.text)
                and commerce_state.order_id
                and not (result.commercial_data or {}).get("payment")
            ):
                payment_result = await inspect_order_payment(
                    state=commerce_state,
                    execute=execute_tool,
                    order_id=commerce_state.order_id,
                )
                if (payment_result.commercial_data or {}).get("payment"):
                    result = payment_result
        return _annotate_agent_result(
            result,
            domain="commerce",
            response_source="deterministic_fallback",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=bool(result.response_metadata.get("used_tray")),
        )
    # Instagram Story reply → associated product (feature-flagged / rollout).
    try:
        from .instagram_story_intent import should_route_story_question
        from .instagram_story_service import (
            resolve_story_product_question,
            story_result_to_agent_result,
        )

        if should_route_story_question(message):
            story_resolution = await resolve_story_product_question(
                incoming=message,
                execute_tool=execute_tool,
            )
            if story_resolution is not None:
                story_agent = story_result_to_agent_result(
                    story_resolution,
                    incoming=message,
                )
                if story_agent is not None:
                    return _annotate_agent_result(
                        story_agent,
                        domain="commerce",
                        goal="inspect",
                        response_source="instagram_story",
                        used_openai_interpreter=False,
                        used_openai_responder=False,
                        used_tray=bool(story_resolution.product_payload),
                        fallback_reason=story_resolution.failure_reason,
                    )
    except Exception as exc:  # noqa: BLE001
        print("[instagram.story.route.error]", {"error_type": type(exc).__name__})

    if image_search_eligible(message):
        image_result = await handle_image_product_search(message)
        if image_result is not None:
            return _annotate_agent_result(
                image_result,
                domain="commerce",
                goal="find",
                response_source=(
                    "technical_fallback"
                    if image_result.safety_reason
                    in {
                        "image_identify_failed",
                        "tray_adapter_unavailable",
                        "product_match_failed",
                    }
                    else image_result.response_metadata.get(
                        "response_source",
                        "image_vision",
                    )
                ),
                used_openai_interpreter=False,
                used_openai_responder=bool(
                    image_result.response_metadata.get("used_openai_responder")
                ),
                used_tray=bool(image_result.response_metadata.get("used_tray")),
                fallback_reason=image_result.safety_reason,
            )

    # Brevo cannot deliver Instagram Story / some IG media URLs — guide resend.
    try:
        from .brevo_instagram_media import (
            PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
            UNVIEWABLE_MEDIA_GUIDE_REPLY,
            is_brevo_unviewable_media_text,
            should_guide_instagram_price_without_media,
        )

        if is_brevo_unviewable_media_text(message.text):
            return _annotate_agent_result(
                AgentResult(
                    reply_text=UNVIEWABLE_MEDIA_GUIDE_REPLY,
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="instagram_media_unviewable",
                ),
                domain="commerce",
                goal="inspect",
                response_source="deterministic_fallback",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="brevo_instagram_media_unviewable",
            )
        if (
            should_guide_instagram_price_without_media(message)
            and getattr(commerce_state, "active_product", None) is None
        ):
            return _annotate_agent_result(
                AgentResult(
                    reply_text=PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
                    intent="commerce",
                    handoff_required=False,
                    safety_reason="instagram_media_unviewable",
                ),
                domain="commerce",
                goal="inspect",
                response_source="deterministic_fallback",
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
                fallback_reason="instagram_price_without_media",
            )
    except Exception as exc:  # noqa: BLE001
        print("[brevo.instagram_media.guide.error]", {"error_type": type(exc).__name__})

    interpretation = await interpret_message(
        message,
        recent_turns=model_turns,
        commerce_state=commerce_state,
    )
    used_openai_interpreter = interpretation._source == "openai"
    interpreted_domain = interpretation.domain
    interpretation, domain_context_applied = apply_commerce_domain_context(
        interpretation,
        commerce_state,
    )
    print("[sales.domain.context]", {
        "previous_domain": commerce_state.active_domain,
        "interpreted_domain": interpreted_domain,
        "domain_changed": bool(
            commerce_state.active_domain
            and commerce_state.active_domain != interpretation.domain
        ),
        "change_explicit": interpretation.domain_change_explicit,
        "context_override": domain_context_applied,
    })
    primary_intent = detect_primary_intent(message.text)
    raffle_intents = {"balance", "coupon_code", "simulation", "raffle_history", "current_raffle", "rules"}
    scope_domain = (
        "raffle"
        if not used_openai_interpreter and primary_intent in raffle_intents
        else interpretation.domain
    )
    print("[agent.scope]", {"domain": scope_domain})
    if scope_domain == "out_of_scope":
        return _annotate_agent_result(
            AgentResult(reply_text=OUT_OF_SCOPE_REPLY, intent="out_of_scope", handoff_required=False, safety_reason="scope_refusal"),
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="guardrail" if used_openai_interpreter else "deterministic_fallback",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=interpretation._fallback_reason,
        )
    if scope_domain == "greeting" or (
        interpretation._source != "openai"
        and is_soft_greeting(message.text)
        and has_resumable_commerce(commerce_state)
    ):
        if has_resumable_commerce(commerce_state):
            resume = build_contextual_greeting(commerce_state)
            return _annotate_agent_result(
                resume,
                domain=resume.response_metadata.get("domain") or "commerce",
                response_source=resume.response_metadata.get(
                    "response_source",
                    "context_resume",
                ),
                used_openai_interpreter=False,
                used_openai_responder=False,
                used_tray=False,
                fallback_reason=interpretation._fallback_reason,
            )
        return _annotate_agent_result(
            AgentResult(
                reply_text=choose_greeting_reply(recent_turns),
                intent="general",
                handoff_required=False,
            ),
            domain="greeting",
            response_source="local_greeting",
            used_openai_interpreter=False,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason=interpretation._fallback_reason,
        )
    print("[agent.route]", {"inbound_id": (message.raw or {}).get("inbound_id"), "primary_intent": primary_intent})
    third_party_reply = _third_party_guardrail(message, primary_intent)
    if third_party_reply:
        return _annotate_agent_result(
            third_party_reply,
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="guardrail",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
        )
    if message.input_modality == "audio" and (message.transcription_failed or not (message.text or "").strip()):
        return _annotate_agent_result(
            generate_agent_reply(message, customer_context),
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="technical_fallback",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
            fallback_reason="audio_transcription_failed",
        )
    facts = gather_customer_facts(message, customer_context)
    facts["scope_domain"] = scope_domain
    if scope_domain == "commerce":
        facts = {**facts, "primary_intent": "commerce", "intents": [*facts.get("intents", []), "commerce"]}
    preferred_reply = _preferred_name_reply_if_requested(message, facts)
    if preferred_reply:
        return _annotate_agent_result(
            preferred_reply,
            domain=scope_domain,
            goal=interpretation.goal,
            response_source="deterministic_fallback",
            used_openai_interpreter=used_openai_interpreter,
            used_openai_responder=False,
            used_tray=False,
        )
    if scope_domain == "raffle":
        local_reply = _local_raffle_reply(message, facts)
        if local_reply:
            return _annotate_agent_result(
                local_reply,
                domain="raffle",
                goal=interpretation.goal,
                response_source="local_raffle",
                used_openai_interpreter=used_openai_interpreter,
                used_openai_responder=False,
                used_tray=False,
            )
        if detect_available_numbers_inquiry(message.text):
            return _annotate_agent_result(
                build_available_numbers_reply(message),
                domain="raffle",
                goal=interpretation.goal,
                response_source="local_raffle",
                used_openai_interpreter=used_openai_interpreter,
                used_openai_responder=False,
                used_tray=False,
            )
    if scope_domain == "commerce":
        commerce_result = await handle_sales_message(
            message,
            facts,
            customer_context,
            interpretation,
            recent_turns=model_turns,
            commerce_state=commerce_state,
        )
        if commerce_result is not None:
            return _annotate_agent_result(
                commerce_result,
                domain="commerce",
                goal=interpretation.goal,
                used_openai_interpreter=used_openai_interpreter,
                fallback_reason=interpretation._fallback_reason,
                interpretation_confidence=interpretation.confidence,
            )
    print("[openai.agent] routing", {"mode": "openai_with_db_context_and_tools", "primary_intent": facts.get("primary_intent"), "has_openai_key": bool(get_settings().openai_api_key), "tray_tools_enabled": bool(get_settings().tray_adapter_url and get_settings().tray_adapter_token)})
    result = await generate_openai_reply_async(message, customer_context, facts)
    return _annotate_agent_result(
        result,
        domain=scope_domain,
        goal=interpretation.goal,
        response_source="technical_fallback" if result.safety_reason else "openai",
        used_openai_interpreter=used_openai_interpreter,
        used_openai_responder=not bool(result.safety_reason),
        used_tray=False,
        fallback_reason=result.safety_reason or interpretation._fallback_reason,
        interpretation_confidence=interpretation.confidence,
    )
