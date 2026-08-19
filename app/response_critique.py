from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Literal

from openai import APIError
from pydantic import BaseModel, Field

from .capability_catalog import (
    RETRYABLE_API_NAMES,
    build_capability_catalog,
    format_capability_catalog_for_prompt,
)
from .commerce_context import (
    CommerceConversationState,
    CommerceProductReference,
    PresentedCommerceProduct,
    product_reference_from_product,
)
from .config import get_settings
from .greeting_policy import is_generic_greeting_reply
from .guardrails import detect_trade_in_or_appraisal_request
from .handoff_service import build_human_handoff_result
from .models import AgentResult, IncomingMessage
from .quality_judge import (
    JudgeReport,
    JudgeVerdict,
    attach_judge_report,
    is_low_risk_judge_skip,
)
from .runtime_context import get_current_turn
from .site_knowledge import TRADE_IN_HANDOFF_MESSAGE
from .tray_tools import execute_tool
from .turn_runtime import LLMCallBudgetExceeded


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

CRITIQUE_JUDGE_SYSTEM_PROMPT = (
    "Você é o JUÍZ redundante do agente NewStore. "
    "Valide se a resposta cumpre o pedido do cliente com o "
    "histórico completo e as capacidades/APIs disponíveis. "
    "pass_check=false se a resposta negar pedido/link/pagamento "
    "existentes no histórico, inventar fatos, ignorar contexto, "
    "ou deixar de consultar API necessária. "
    "Também reprove (pass_check=false) quando o cliente pediu um "
    "tipo, função ou atributo de produto (ex.: cronógrafo, diver, GMT, "
    "automático, cor, orçamento, gênero, marca) e os itens em "
    "commercial_data.products / a resposta NÃO evidenciam esse "
    "requisito nos nomes ou fatos disponíveis — mesmo que sejam "
    "produtos reais da categoria genérica. "
    "Nesses casos, recommended_apis DEVE incluir search_products com "
    "arguments.query refinada em termos de catálogo (português quando "
    "fizer sentido, ex.: 'cronógrafo', 'mergulho', 'GMT'), sem inventar "
    "produtos. "
    "Quando reprovar, liste recommended_apis (somente retryable) "
    "com arguments concretos e retry_instruction objetiva. "
    "Não reescreva a resposta final aqui."
)


class RecommendedApiCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class CritiqueVerdict(BaseModel):
    score: int = Field(default=100, ge=0, le=100)
    pass_check: bool = True
    issues: list[str] = Field(default_factory=list)
    summary: str = ""
    missing_context: list[str] = Field(default_factory=list)
    recommended_apis: list[RecommendedApiCall] = Field(default_factory=list)
    retry_instruction: str = ""
    better_reply_hint: str = ""


class CritiqueLoopReport(BaseModel):
    mode: Literal["off", "shadow", "enforce"] = "off"
    attempts: int = 0
    max_retries: int = 0
    approved: bool = True
    api_calls: list[dict[str, Any]] = Field(default_factory=list)
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    regenerated: bool = False
    applied_handoff: bool = False


def _last_assistant_reply(recent_turns: list[dict[str, Any]] | None) -> str | None:
    for turn in reversed(recent_turns or []):
        if isinstance(turn, dict) and turn.get("role") == "assistant":
            content = str(turn.get("content") or "").strip()
            return content or None
    return None


def _fold_reply(text: str | None) -> str:
    import unicodedata

    value = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def apply_fast_deterministic_critique(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    recent_turns: list[dict[str, Any]] | None = None,
) -> tuple[AgentResult, CritiqueVerdict | None, str | None]:
    """Instant judge for clear gaffes — no LLM latency.

    Returns (possibly fixed result, verdict-or-None, skip_reason-or-None).
    When skip_reason is set, the LLM critique should be skipped entirely.
    When verdict.pass_check is False and result was rewritten, skip LLM too.
    """
    reply = (result.reply_text or "").strip()
    text = (incoming.text or "").strip()

    # 1) Trade-in / appraisal must never be flatly refused by the bot.
    if detect_trade_in_or_appraisal_request(text):
        denied = any(
            cue in reply.casefold()
            for cue in (
                "não compramos",
                "nao compramos",
                "apenas vendemos",
                "só vendemos",
                "so vendemos",
                "não avaliamos",
                "nao avaliamos",
                "não fazemos troca",
                "nao fazemos troca",
            )
        )
        if denied or not result.handoff_required:
            fixed = build_human_handoff_result(reason="trade_in_or_appraisal")
            verdict = CritiqueVerdict(
                score=20,
                pass_check=False,
                issues=["trade_in_policy_violation"],
                summary="Cliente pediu avaliação/troca/compra de usado; handoff obrigatório.",
                better_reply_hint=TRADE_IN_HANDOFF_MESSAGE,
            )
            return fixed, verdict, "fast_trade_in_handoff"

    # 2) Do not re-send the same greeting this person already received.
    previous = _last_assistant_reply(recent_turns)
    if (
        reply
        and previous
        and _fold_reply(reply) == _fold_reply(previous)
        and is_generic_greeting_reply(reply)
    ):
        from .greeting_policy import choose_greeting_reply

        alt = choose_greeting_reply(recent_turns)
        if _fold_reply(alt) == _fold_reply(reply):
            alt = "Pode me dizer o que você precisa?"
        fixed = result.model_copy(deep=True)
        fixed.reply_text = alt
        fixed.response_metadata = dict(fixed.response_metadata or {})
        fixed.response_metadata["fast_critique"] = "deduped_greeting"
        verdict = CritiqueVerdict(
            score=60,
            pass_check=True,
            issues=["duplicate_greeting_rewritten"],
            summary="Mesma saudação já enviada a esta pessoa; reescrita sem LLM.",
        )
        return fixed, verdict, "fast_greeting_dedupe"

    # 3) Empty exact-product denial when customer gave gender+budget preferences.
    if reply.casefold().startswith("não encontrei esse produto") or reply.casefold().startswith(
        "nao encontrei esse produto"
    ):
        genderish = any(
            token in text.casefold()
            for token in ("feminino", "masculino", "unissex", "dama", "mulher", "homem")
        )
        budgetish = any(
            token in text.casefold() for token in ("até", "ate", "reais", "r$", "mil")
        )
        if genderish or budgetish:
            verdict = CritiqueVerdict(
                score=35,
                pass_check=False,
                issues=["preference_search_treated_as_exact_product"],
                summary=(
                    "Cliente deu preferências (gênero/orçamento), não um modelo exato. "
                    "Reconsultar catálogo com query refinada."
                ),
                recommended_apis=[
                    RecommendedApiCall(
                        name="search_products",
                        arguments={
                            "query": text[:120],
                            "limit": 20,
                        },
                        reason="retry_gender_budget_recommendation",
                    )
                ],
                retry_instruction=(
                    "Busque recomendações por categoria/gênero/orçamento; "
                    "não trate a mensagem como modelo exato."
                ),
            )
            # Do not skip LLM regeneration path — return fail so retry loop runs.
            return result, verdict, None

    return result, None, None


def _transcript_blob(recent_turns: list[dict[str, Any]] | None, *, max_chars: int = 12000) -> str:
    parts: list[str] = []
    for turn in recent_turns or []:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "unknown")
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"{role}: {content}")
    blob = "\n".join(parts)
    if len(blob) <= max_chars:
        return blob
    return blob[-max_chars:]


def _seed_args_from_context(
    *,
    state: CommerceConversationState | None,
    result: AgentResult,
) -> dict[str, Any]:
    commerce = result.commercial_data or {}
    payment = commerce.get("payment") if isinstance(commerce.get("payment"), dict) else {}
    order_id = (
        commerce.get("order_id")
        or (state.order_id if state else None)
        or (state.order_lookup_id if state else None)
    )
    session_id = (
        commerce.get("session_id")
        or (state.cart_session_id if state else None)
        or (state.order_session_id if state else None)
    )
    customer = state.checkout_draft.customer if state else None
    product = state.active_product if state and state.active_product else None
    if isinstance(product, dict):
        product_id = product.get("product_id") or product.get("id")
        product_name = product.get("name")
    elif product is not None:
        product_id = getattr(product, "product_id", None) or getattr(product, "id", None)
        product_name = getattr(product, "name", None)
    else:
        product_id = None
        product_name = None
    return {
        "order_id": str(order_id).strip() if order_id else None,
        "session_id": str(session_id).strip() if session_id else None,
        "cart_session_id": str(session_id).strip() if session_id else None,
        "payment_url": (
            payment.get("payment_url")
            or (state.order_payment_url if state else None)
        ),
        "cpf": (customer.cpf if customer else None),
        "email": (customer.email if customer else None),
        "product_id": commerce.get("product_id") or product_id,
        "query": commerce.get("query") or product_name,
    }


def _fill_api_arguments(
    call: RecommendedApiCall,
    seeds: dict[str, Any],
) -> dict[str, Any] | None:
    name = str(call.name or "").strip()
    if name not in RETRYABLE_API_NAMES:
        return None
    args = dict(call.arguments or {})
    defaults: dict[str, dict[str, Any]] = {
        "get_order_complete": {"order_id": seeds.get("order_id")},
        "get_order": {"order_id": seeds.get("order_id")},
        "get_order_payment": {"order_id": seeds.get("order_id")},
        "get_payment_options": (
            {"order_id": seeds.get("order_id")}
            if seeds.get("order_id")
            else {"cart_session_id": seeds.get("cart_session_id")}
        ),
        "get_cart": {"session_id": seeds.get("session_id")},
        "get_cart_complete": {"session_id": seeds.get("session_id")},
        "list_orders": (
            {"session_id": seeds.get("session_id")}
            if seeds.get("session_id")
            else {"limit": 5}
        ),
        "search_customer": (
            {"cpf": seeds.get("cpf")}
            if seeds.get("cpf")
            else {"email": seeds.get("email")}
        ),
        "get_product": {"product_id": seeds.get("product_id")},
        "get_product_link": {"product_id": seeds.get("product_id")},
        "check_inventory": {"product_id": seeds.get("product_id")},
        "search_products": {"query": seeds.get("query"), "limit": 5},
    }
    for key, value in (defaults.get(name) or {}).items():
        if args.get(key) in (None, "") and value not in (None, ""):
            args[key] = value
    # Drop empty values.
    cleaned = {key: value for key, value in args.items() if value not in (None, "", [])}
    if name in {"get_order_complete", "get_order", "get_order_payment"} and not cleaned.get(
        "order_id"
    ):
        return None
    if name in {"get_cart", "get_cart_complete"} and not cleaned.get("session_id"):
        return None
    if name == "search_customer" and not (cleaned.get("cpf") or cleaned.get("email")):
        return None
    if name in {"get_product", "get_product_link", "check_inventory"} and not cleaned.get(
        "product_id"
    ):
        return None
    if name == "search_products" and not cleaned.get("query"):
        return None
    if name == "get_payment_options" and not (
        cleaned.get("order_id") or cleaned.get("cart_session_id")
    ):
        return None
    return cleaned


async def run_critique_judge(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    recent_turns: list[dict[str, Any]] | None,
    commerce_state: CommerceConversationState | None,
) -> CritiqueVerdict:
    settings = get_settings()
    catalog = build_capability_catalog()
    if not settings.openai_api_key:
        return CritiqueVerdict(
            score=50,
            pass_check=True,
            issues=["openai_unavailable"],
            summary="Critique skipped; OpenAI unavailable.",
        )
    payload = {
        "customer_message": incoming.text,
        "agent_reply": result.reply_text,
        "intent": result.intent,
        "safety_reason": result.safety_reason,
        "commercial_data": result.commercial_data or {},
        "working_memory": (
            result.response_metadata.get("working_memory")
            or {}
        ),
        "commerce_state": (
            commerce_state.interpreter_payload() if commerce_state else {}
        ),
        "conversation_transcript": _transcript_blob(recent_turns),
        "available_apis": catalog.get("apis"),
        "retryable_apis": catalog.get("retryable_apis"),
        "policy": catalog.get("policy"),
    }
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import parse_structured_output

        parse_result = await parse_structured_output(
            model=settings.openai_model,
            text_format=CritiqueVerdict,
            messages=[
                {
                    "role": "system",
                    "content": CRITIQUE_JUDGE_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ],
            temperature=0,
            call_type="judge",
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, CritiqueVerdict):
            raise ValueError("critique_schema_missing")
        return parsed
    except (
        APIError,
        OpenAIGatewayError,
        LLMCallBudgetExceeded,
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        return CritiqueVerdict(
            score=50,
            pass_check=True,
            issues=[f"critique_failed:{type(exc).__name__}"],
            summary="Critique failed open; keep original reply.",
        )


async def _execute_recommended_apis(
    *,
    verdict: CritiqueVerdict,
    seeds: dict[str, Any],
    execute: ToolExecutor,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    gathered: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    for item in verdict.recommended_apis[:4]:
        args = _fill_api_arguments(item, seeds)
        if not args:
            calls.append(
                {
                    "name": item.name,
                    "skipped": True,
                    "reason": "missing_arguments_or_not_retryable",
                }
            )
            continue
        try:
            payload = await execute(item.name, args)
        except Exception as exc:
            payload = {"error": "critique_tool_failed", "error_type": type(exc).__name__}
        calls.append(
            {
                "name": item.name,
                "arguments_keys": sorted(args.keys()),
                "ok": "error" not in payload,
            }
        )
        gathered[item.name] = payload
        # Refresh seeds from successful order lookups.
        if item.name in {"get_order_complete", "get_order"} and "error" not in payload:
            order_id = payload.get("order_id") or payload.get("id")
            if order_id:
                seeds["order_id"] = str(order_id)
            payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else {}
            if payment.get("payment_url"):
                seeds["payment_url"] = payment.get("payment_url")
        if item.name == "get_order_payment" and "error" not in payload:
            payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else payload
            if isinstance(payment, dict) and payment.get("payment_url"):
                seeds["payment_url"] = payment.get("payment_url")
        if item.name == "search_products" and "error" not in payload:
            if args.get("query"):
                seeds["query"] = args.get("query")
    return calls, gathered


def _products_from_search_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Return product list from search_products payload, or None if not a search result."""
    if not isinstance(payload, dict) or "error" in payload:
        return None
    products = payload.get("products")
    if not isinstance(products, list):
        return None
    return [item for item in products if isinstance(item, dict)]


def apply_search_products_to_result(
    *,
    result: AgentResult,
    api_facts: dict[str, Any],
    commerce_state: CommerceConversationState | None = None,
    search_query: str | None = None,
) -> AgentResult:
    """Replace commercial_data.products with critique search_products evidence."""
    products = _products_from_search_payload(
        api_facts.get("search_products") if isinstance(api_facts, dict) else None
    )
    if products is None:
        return result
    updated = result.model_copy(deep=True)
    commercial = dict(updated.commercial_data or {})
    commercial["products"] = products
    if search_query:
        commercial["query"] = search_query
    if not products:
        commercial.pop("inventory", None)
    updated.commercial_data = commercial
    metadata = dict(updated.response_metadata or {})
    metadata["presented_products"] = bool(products)
    metadata["critique_products_replaced"] = True
    if not products:
        metadata["product_resolution_state"] = "not_found"
        metadata["clear_active_product"] = True
    updated.response_metadata = metadata
    if commerce_state is not None:
        compact: list[PresentedCommerceProduct] = []
        for position, product in enumerate(products[:3], start=1):
            identity = product_reference_from_product(product)
            if identity:
                compact.append(
                    PresentedCommerceProduct(position=position, **identity.model_dump())
                )
        commerce_state.last_presented_products = compact
        if compact:
            commerce_state.active_product = CommerceProductReference.model_validate(
                compact[0].model_dump(exclude={"position"})
            )
            commerce_state.product_resolution_state = "plausible_matches"
        else:
            commerce_state.active_product = None
            commerce_state.product_resolution_state = "not_found"
    return updated


def _merge_payment_and_order_facts(
    commercial: dict[str, Any],
    api_facts: dict[str, Any],
    commerce_state: CommerceConversationState | None,
) -> dict[str, Any]:
    commercial = dict(commercial)
    commercial["critique_api_facts"] = {
        key: ("error" not in value) if isinstance(value, dict) else True
        for key, value in api_facts.items()
    }
    payment_url = None
    for payload in api_facts.values():
        if not isinstance(payload, dict):
            continue
        payment = payload.get("payment")
        if isinstance(payment, dict) and payment.get("payment_url"):
            payment_url = payment.get("payment_url")
        elif payload.get("payment_url"):
            payment_url = payload.get("payment_url")
    if payment_url:
        payment_block = dict(commercial.get("payment") or {})
        payment_block["payment_url"] = payment_url
        commercial["payment"] = payment_block
        if commerce_state is not None:
            commerce_state.order_payment_url = str(payment_url)
    order_id = None
    for payload in api_facts.values():
        if isinstance(payload, dict) and (
            payload.get("order_id") or payload.get("id")
        ):
            order_id = payload.get("order_id") or payload.get("id")
            break
    if order_id:
        commercial["order_id"] = str(order_id)
    return commercial


async def _regenerate_reply(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    verdict: CritiqueVerdict,
    api_facts: dict[str, Any],
    recent_turns: list[dict[str, Any]] | None,
    commerce_state: CommerceConversationState | None,
) -> AgentResult | None:
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import generate_text_output

        search_query = None
        for item in verdict.recommended_apis:
            if item.name == "search_products":
                query = (item.arguments or {}).get("query")
                if query:
                    search_query = str(query)
                    break
        working = apply_search_products_to_result(
            result=result,
            api_facts=api_facts,
            commerce_state=commerce_state,
            search_query=search_query,
        )
        products = (working.commercial_data or {}).get("products")
        search_ran = "search_products" in (api_facts or {})
        empty_search = (
            search_ran
            and isinstance(products, list)
            and len(products) == 0
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "Você é o agente de RESPOSTA da NewStore. "
                    "Regenera a resposta ao cliente usando o histórico, os fatos "
                    "já conhecidos e os novos resultados de API. "
                    "Não invente dados. Se houver payment_url nos fatos, envie o link. "
                    "Se commercial_data.products foi atualizado pela reconsulta, "
                    "apresente SOMENTE esses produtos (não os da resposta anterior). "
                    "Se a reconsulta search_products veio vazia, diga com honestidade "
                    "que não encontrou o que o cliente pediu — nunca reenvie a lista "
                    "anterior inadequada. "
                    "Resposta curta em português do Brasil para WhatsApp.\n\n"
                    + format_capability_catalog_for_prompt()
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "customer_message": incoming.text,
                        "previous_reply": result.reply_text,
                        "judge_issues": verdict.issues,
                        "missing_context": verdict.missing_context,
                        "retry_instruction": verdict.retry_instruction,
                        "better_reply_hint": verdict.better_reply_hint,
                        "conversation_transcript": _transcript_blob(recent_turns),
                        "commerce_state": (
                            commerce_state.interpreter_payload() if commerce_state else {}
                        ),
                        "commercial_data": working.commercial_data or {},
                        "search_products_empty": empty_search,
                        "api_facts": api_facts,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ]
        text_result = await generate_text_output(
            model=settings.openai_model,
            messages=messages,
            temperature=0.2,
            call_type="response_composition",
        )
        content = text_result.text
        if not content or not content.strip():
            return None
        regenerated = working.model_copy(deep=True)
        regenerated.reply_text = content.strip()
        commercial = dict(regenerated.commercial_data or {})
        if api_facts:
            commercial = _merge_payment_and_order_facts(
                commercial,
                api_facts,
                commerce_state,
            )
        regenerated.commercial_data = commercial
        regenerated.response_metadata = dict(regenerated.response_metadata or {})
        regenerated.response_metadata["critique_regenerated"] = True
        return regenerated
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError):
        return None


async def apply_response_critique_loop(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    recent_turns: list[dict[str, Any]] | None = None,
    commerce_state: CommerceConversationState | None = None,
    mode: Literal["off", "shadow", "enforce"] | None = None,
    max_retries: int | None = None,
    execute: ToolExecutor | None = None,
    risk_score: int = 0,
    factual_valid: bool = True,
    openai_call_count: int = 0,
) -> tuple[AgentResult, CritiqueLoopReport]:
    """Dual-agent critique: judge draft, optionally call APIs and regenerate before send."""
    settings = get_settings()
    if mode is not None:
        critique_mode: Literal["off", "shadow", "enforce"] = mode
    else:
        from .rollout import resolve_effective_critique_mode

        resolved = resolve_effective_critique_mode(settings)
        critique_mode = (  # type: ignore[assignment]
            resolved if resolved in {"off", "shadow", "enforce"} else "shadow"
        )
    retries = (
        max_retries
        if max_retries is not None
        else int(getattr(settings, "agent_critique_max_retries", 2))
    )
    report = CritiqueLoopReport(mode=critique_mode, max_retries=retries)
    if critique_mode == "off":
        return result, report

    skip, skip_reason = is_low_risk_judge_skip(incoming, result)
    if skip:
        result.response_metadata = dict(result.response_metadata or {})
        result.response_metadata["response_critique"] = {
            **report.model_dump(mode="json"),
            "skipped": True,
            "skip_reason": skip_reason,
        }
        print("[agent.critique.skip]", {"reason": skip_reason})
        return result, report

    # Instant deterministic checks (no LLM) — keeps latency low for clear gaffes.
    fast_result, fast_verdict, fast_skip = apply_fast_deterministic_critique(
        incoming=incoming,
        result=result,
        recent_turns=recent_turns,
    )
    if fast_skip:
        report.attempts = 1
        report.approved = True
        if fast_verdict is not None:
            report.verdicts.append(fast_verdict.model_dump(mode="json"))
        fast_result.response_metadata = dict(fast_result.response_metadata or {})
        fast_result.response_metadata["response_critique"] = {
            **report.model_dump(mode="json"),
            "skipped": True,
            "skip_reason": fast_skip,
            "fast_path": True,
        }
        print("[agent.critique.fast]", {"reason": fast_skip})
        return fast_result, report

    seeded_fail = bool(fast_verdict is not None and not fast_verdict.pass_check)
    if seeded_fail:
        # Seed the LLM retry loop with a deterministic fail (one attempt max feel).
        result = fast_result
        report.verdicts.append(fast_verdict.model_dump(mode="json"))

    from .llm_call_policy import should_run_llm_critique

    run_llm, gate_reason, gate_signals = should_run_llm_critique(
        incoming=incoming,
        result=result,
        critique_mode=critique_mode,
        risk_score=risk_score,
        factual_valid=factual_valid,
        openai_call_count=openai_call_count,
    )
    # Deterministic fail always warrants the LLM path in enforce (and observe in shadow).
    if seeded_fail:
        run_llm = True
        gate_reason = "deterministic_seed_fail"
    if not run_llm:
        result.response_metadata = dict(result.response_metadata or {})
        result.response_metadata["response_critique"] = {
            **report.model_dump(mode="json"),
            "skipped": True,
            "skip_reason": gate_reason,
            "risk_gate": True,
            "risk_signals": gate_signals,
        }
        print(
            "[agent.critique.skip]",
            {"reason": gate_reason, "signals": gate_signals[:8]},
        )
        return result, report

    try:
        return await _run_response_critique_loop(
            incoming=incoming,
            result=result,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
            critique_mode=critique_mode,
            retries=retries,
            report=report,
            execute=execute,
            seed_verdict=fast_verdict if seeded_fail else None,
        )
    except Exception as exc:
        # Critique must never take down the WhatsApp reply path.
        print("[agent.critique.error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })
        result.response_metadata["response_critique"] = {
            **report.model_dump(mode="json"),
            "error_type": type(exc).__name__,
            "skipped": True,
        }
        return result, report


async def _run_response_critique_loop(
    *,
    incoming: IncomingMessage,
    result: AgentResult,
    recent_turns: list[dict[str, Any]] | None,
    commerce_state: CommerceConversationState | None,
    critique_mode: Literal["off", "shadow", "enforce"],
    retries: int,
    report: CritiqueLoopReport,
    execute: ToolExecutor | None,
    seed_verdict: CritiqueVerdict | None = None,
) -> tuple[AgentResult, CritiqueLoopReport]:
    executor = execute or execute_tool
    current = result
    seeds = _seed_args_from_context(state=commerce_state, result=current)
    attempt = 0
    while True:
        attempt += 1
        report.attempts = attempt
        if attempt == 1 and seed_verdict is not None:
            verdict = seed_verdict
            # Already recorded in report.verdicts by caller when seeded.
            if not report.verdicts or report.verdicts[-1].get("issues") != seed_verdict.issues:
                report.verdicts.append(seed_verdict.model_dump(mode="json"))
        else:
            verdict = await run_critique_judge(
                incoming=incoming,
                result=current,
                recent_turns=recent_turns,
                commerce_state=commerce_state,
            )
            report.verdicts.append(verdict.model_dump(mode="json"))
        print("[agent.critique]", {
            "attempt": attempt,
            "pass_check": verdict.pass_check,
            "score": verdict.score,
            "issues": verdict.issues[:5],
            "recommended_apis": [item.name for item in verdict.recommended_apis[:4]],
            "seeded": bool(attempt == 1 and seed_verdict is not None),
        })
        if verdict.pass_check:
            report.approved = True
            break

        if critique_mode == "shadow" or attempt > retries:
            report.approved = False
            if critique_mode == "enforce" and attempt > retries:
                current.reply_text = (
                    "Prefiro confirmar esses dados com a equipe antes de te responder "
                    "com segurança. Um atendente humano pode te ajudar agora."
                )
                current.handoff_required = True
                current.safety_reason = "response_critique_failed"
                report.applied_handoff = True
            break

        api_calls, api_facts = await _execute_recommended_apis(
            verdict=verdict,
            seeds=seeds,
            execute=executor,
        )
        report.api_calls.extend(api_calls)
        regenerated = await _regenerate_reply(
            incoming=incoming,
            result=current,
            verdict=verdict,
            api_facts=api_facts,
            recent_turns=recent_turns,
            commerce_state=commerce_state,
        )
        if regenerated is None:
            report.approved = False
            current.reply_text = (
                "Prefiro confirmar esses dados com a equipe antes de te responder "
                "com segurança. Um atendente humano pode te ajudar agora."
            )
            current.handoff_required = True
            current.safety_reason = "response_critique_regenerate_failed"
            report.applied_handoff = True
            break
        current = regenerated
        report.regenerated = True
        seeds = _seed_args_from_context(state=commerce_state, result=current)

    # Mirror into legacy quality_judge metadata for observability continuity.
    last = report.verdicts[-1] if report.verdicts else None
    if last is not None:
        attach_judge_report(
            current,
            JudgeReport(
                triggered=True,
                mode=critique_mode,
                reason="response_critique",
                verdict=JudgeVerdict(
                    score=int(last.get("score") or 0),
                    pass_check=bool(last.get("pass_check")),
                    issues=list(last.get("issues") or []),
                    summary=str(last.get("summary") or ""),
                ),
                applied=report.applied_handoff,
            ),
        )
    current.response_metadata["response_critique"] = report.model_dump(mode="json")
    runtime = get_current_turn()
    if runtime is not None:
        runtime.judge_mode = critique_mode
        runtime.judge_triggered = True
        if report.applied_handoff:
            runtime.register_fallback("response_critique_failed")
    return current, report
