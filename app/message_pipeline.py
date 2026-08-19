from __future__ import annotations

from app.config import get_settings
from app.agent_contracts import build_agent_decision, evaluate_policy
from app.commerce_context import CommerceConversationState, evolve_commerce_state
from app.customer_identity import (
    resolve_person_key_candidates,
    upsert_customer_identity_links,
)
from app.db import (
    load_commerce_conversation_state,
    load_recent_conversation_turns,
    persist_customer_commerce_session,
)
from app.observability import (
    log_event,
    log_exception,
    redact_text,
    summarize_commerce_state,
    summarize_customer_context,
)
from app.working_memory import build_working_memory
from app.factual_validator import apply_factual_validation
from app.handoff_service import enrich_handoff_metadata
from app.models import AgentResult, IncomingMessage
from app.openai_agent import generate_agent_reply_async
from app.quality_judge import attach_judge_report, run_quality_judge
from app.response_composer import compose_outbound_reply
from app.response_critique import apply_response_critique_loop
from app.runtime_context import get_current_turn, runtime_stage
from app.user_preferences import enrich_customer_context, learn_from_incoming_message, record_interaction_memory
from app.audio_service import should_transcribe_incoming


async def prepare_incoming_message(incoming: IncomingMessage) -> IncomingMessage:
    settings = get_settings()
    if not settings.audio_inbound_enabled:
        return incoming
    if not should_transcribe_incoming(incoming.text, incoming.audio_url, incoming.audio_filename):
        return incoming

    from app.audio_service import transcribe_audio_url

    try:
        transcribed = await transcribe_audio_url(
            incoming.audio_url or "",
            filename=incoming.audio_filename,
        )
    except Exception as exc:
        log_exception(
            "audio.inbound.transcription_failed",
            exc,
            {"has_audio_url": bool(incoming.audio_url)},
        )
        incoming.transcription_failed = True
        incoming.text = ""
        incoming.input_modality = "audio"
        return incoming

    log_event(
        "audio.inbound.transcribed",
        {
            "chars": len(transcribed),
            "preview": redact_text(transcribed, max_chars=400),
        },
    )
    incoming.text = transcribed
    incoming.input_modality = "audio"
    return incoming


async def enrich_agent_result(incoming: IncomingMessage, result: AgentResult) -> AgentResult:
    settings = get_settings()
    if incoming.input_modality != "audio":
        return result
    if not settings.audio_outbound_enabled:
        return result

    from app.audio_service import synthesize_reply_audio
    from app.supabase_storage import upload_public_audio

    try:
        audio_bytes, mime_type, filename = synthesize_reply_audio(result.reply_text)
        audio_url = await upload_public_audio(audio_bytes, content_type=mime_type, filename=filename)
    except Exception as exc:
        log_exception("audio.outbound.tts_or_upload_failed", exc)
        return result

    result.reply_modality = "audio"
    result.reply_audio_bytes = audio_bytes
    result.reply_audio_mime_type = mime_type
    result.reply_audio_url = audio_url
    return result


async def process_incoming_message(incoming: IncomingMessage, customer_context: dict) -> AgentResult:
    settings = get_settings()
    runtime = get_current_turn()
    if runtime is not None:
        runtime.channel = incoming.channel or runtime.channel
        runtime.conversation_key = (
            incoming.conversation_id
            or incoming.sender_key
            or incoming.visitor_id
            or incoming.sender_phone
            or runtime.conversation_key
        )
        try:
            runtime.inbound_id = int((incoming.raw or {}).get("inbound_id"))
        except (TypeError, ValueError):
            pass
    customer_context = enrich_customer_context(customer_context)
    with runtime_stage("prepare_incoming"):
        incoming = await prepare_incoming_message(incoming)
    raw_inbound_id = (incoming.raw or {}).get("inbound_id")
    try:
        inbound_id = int(raw_inbound_id) if raw_inbound_id is not None else None
    except (TypeError, ValueError):
        inbound_id = None
    inbound_snapshot = {
        "channel": incoming.channel,
        "conversation_id_present": bool(incoming.conversation_id),
        "sender_key_present": bool(incoming.sender_key),
        "sender_phone_present": bool(incoming.sender_phone),
        "sender_name_present": bool(incoming.sender_name),
        "visitor_id_present": bool(incoming.visitor_id),
        "input_modality": incoming.input_modality,
        "attachment_type": incoming.attachment_type,
        "image_url_present": bool((incoming.image_url or "").strip()),
        "audio_url_present": bool(incoming.audio_url),
        "text_chars": len(incoming.text or ""),
        "text_preview": redact_text(incoming.text, max_chars=500),
        "customer_found": bool(customer_context.get("found")),
        "customer_context": summarize_customer_context(customer_context),
    }
    if runtime is not None:
        runtime.inbound_snapshot = inbound_snapshot
    log_event("turn.start", inbound_snapshot)
    state_lookup = {
        "conversation_id": incoming.conversation_id,
        "sender_phone": incoming.sender_phone,
        "before_inbound_id": inbound_id,
        "sender_key": incoming.sender_key,
    }
    with runtime_stage("load_context"):
        commerce_state = CommerceConversationState.from_payload(
            load_commerce_conversation_state(**state_lookup)
        )
    # New product photo starts a fresh identification — never price the
    # previous SKU (e.g. CW Rosa) while Vision runs on a Beaubleu.
    if (incoming.image_url or "").strip():
        if commerce_state.active_product is not None:
            log_event(
                "sales.context.clear_active_for_image",
                {
                    "had_active_product_id": commerce_state.active_product.product_id,
                },
            )
        commerce_state.active_product = None
    working_memory = build_working_memory(commerce_state)
    customer_context = {
        **customer_context,
        "_commerce_state": commerce_state.model_dump(mode="json"),
        "_working_memory": working_memory,
    }
    context_snapshot = {
        **summarize_commerce_state(commerce_state),
        "working_memory_payment_pending": bool(working_memory.get("payment_pending")),
        "working_memory_has_open_order": bool(working_memory.get("has_open_order")),
        "working_memory_keys": sorted(str(k) for k in working_memory.keys())[:30],
        "person_key_aliases": len(
            resolve_person_key_candidates(
                sender_key=incoming.sender_key,
                sender_phone=incoming.sender_phone,
                state=commerce_state,
            )
        ),
        "customer_context": summarize_customer_context(customer_context),
    }
    if runtime is not None:
        runtime.context_snapshot = context_snapshot
    log_event("context.loaded", context_snapshot)
    log_event(
        "sales.context.state",
        {
            "active_domain": commerce_state.active_domain,
            "has_active_product": commerce_state.active_product is not None,
            "presented_product_count": len(commerce_state.last_presented_products),
            "active_topic_present": bool(commerce_state.active_topic),
            "purchase_stage": commerce_state.purchase_stage,
            "has_cart_session": bool(commerce_state.cart_session_id),
            "pending_action": commerce_state.pending_action,
            "pending_action_has_product": bool(commerce_state.pending_action_product_ids),
            "has_open_order": bool(working_memory.get("has_open_order")),
            "payment_pending": bool(working_memory.get("payment_pending")),
            **summarize_commerce_state(commerce_state),
        },
    )

    user_id = customer_context.get("user_id")
    if customer_context.get("found") and user_id:
        learn_from_incoming_message(
            int(user_id),
            incoming.text,
            customer_context.get("name"),
        )
        customer_context = enrich_customer_context(customer_context)

    with runtime_stage("agent_decision"):
        result = await generate_agent_reply_async(incoming, customer_context)
    commerce_state = evolve_commerce_state(commerce_state, result)
    result.response_metadata["commerce_state"] = commerce_state.model_dump(mode="json")
    result.response_metadata["working_memory"] = build_working_memory(commerce_state)
    upsert_customer_identity_links(incoming, commerce_state)
    persist_customer_commerce_session(
        person_keys=resolve_person_key_candidates(
            sender_key=incoming.sender_key,
            sender_phone=incoming.sender_phone,
            state=commerce_state,
        ),
        commerce_state=commerce_state.model_dump(mode="json"),
        channel=incoming.channel,
        conversation_id=incoming.conversation_id,
        sender_key=incoming.sender_key,
        sender_phone=incoming.sender_phone,
    )
    decision = build_agent_decision(
        incoming,
        result,
        openai_call_count=runtime.openai_call_count if runtime else 0,
    )
    policy_snapshot = evaluate_policy(
        decision,
        mode=getattr(settings, "agent_policy_mode", "shadow"),
    )
    result.response_metadata["decision_snapshot"] = (
        policy_snapshot.model_dump(mode="json")
    )
    trusted_fact_domains = {
        domain.strip().lower()
        for domain in getattr(
            settings,
            "agent_trusted_fact_domains",
            "sorteionewstore.com.br,newstoresorteios.com.br",
        ).split(",")
        if domain.strip()
    }
    result = apply_factual_validation(
        result,
        decision=decision,
        mode=getattr(
            settings,
            "agent_factual_validation_mode",
            "enforce",
        ),
        trusted_domains=trusted_fact_domains,
        commerce_state=commerce_state.model_dump(mode="json"),
    )
    result = enrich_handoff_metadata(incoming, result)
    validation = result.response_metadata.get("factual_validation") or {}
    from .rollout import (
        resolve_effective_critique_mode,
        resolve_effective_judge_mode,
    )

    critique_mode = resolve_effective_critique_mode(settings)
    if critique_mode not in {"off", "shadow", "enforce"}:
        critique_mode = "shadow"
    judge_mode = resolve_effective_judge_mode(settings)
    if judge_mode not in {"off", "shadow", "enforce"}:
        judge_mode = "off"
    from .history_window import select_model_history_turns
    from .llm_call_policy import should_run_quality_judge

    model_turns = customer_context.get("_model_conversation_turns")
    if not model_turns:
        operational_turns = customer_context.get("_conversation_turns")
        if not operational_turns:
            operational_turns = load_recent_conversation_turns(
                conversation_id=incoming.conversation_id,
                sender_phone=incoming.sender_phone,
                before_inbound_id=inbound_id,
                limit=int(getattr(settings, "agent_history_hard_cap", 80)),
                sender_key=incoming.sender_key,
                hard_cap=int(getattr(settings, "agent_history_hard_cap", 80)),
            )
        model_turns = select_model_history_turns(
            operational_turns,
            limit=int(getattr(settings, "agent_history_limit", 12)),
        )
    factual_ok = bool(validation.get("valid", True))
    openai_calls = runtime.openai_call_count if runtime else 0
    # Dual-agent critique runs before outbound compose/send; LLM gated by risk.
    with runtime_stage("response_critique"):
        judge_report = None
        critique_report = None
        if critique_mode != "off":
            result, critique_report = await apply_response_critique_loop(
                incoming=incoming,
                result=result,
                recent_turns=model_turns,
                commerce_state=commerce_state,
                mode=critique_mode,
                max_retries=int(getattr(settings, "agent_critique_max_retries", 1)),
                risk_score=decision.risk.score,
                factual_valid=factual_ok,
                openai_call_count=openai_calls,
            )
            if critique_report.applied_handoff and runtime is not None:
                runtime.register_fallback("response_critique_failed")
            # Critique may regenerate wording/products — re-validate before send.
            result = apply_factual_validation(
                result,
                decision=decision,
                mode=getattr(
                    settings,
                    "agent_factual_validation_mode",
                    "enforce",
                ),
                trusted_domains=trusted_fact_domains,
                commerce_state=commerce_state.model_dump(mode="json"),
            )
            validation = result.response_metadata.get("factual_validation") or {}
            result.response_metadata["factual_validation_post_critique"] = True
            factual_ok = bool(validation.get("valid", True))
        # Quality judge stays off by default; when enabled, only on risk/sample.
        run_judge, judge_gate_reason, _judge_signals = should_run_quality_judge(
            incoming=incoming,
            result=result,
            judge_mode=judge_mode,
            risk_score=decision.risk.score,
            factual_valid=factual_ok,
            openai_call_count=openai_calls,
        )
        if run_judge and critique_mode == "off":
            judge_report = await run_quality_judge(
                incoming,
                result,
                mode=judge_mode,
                risk_score=decision.risk.score,
                factual_valid=factual_ok,
                openai_call_count=openai_calls,
            )
            result = attach_judge_report(result, judge_report)
        elif judge_mode != "off":
            result.response_metadata = dict(result.response_metadata or {})
            result.response_metadata["quality_judge_gate"] = {
                "run": run_judge,
                "reason": judge_gate_reason,
                "critique_mode": critique_mode,
            }
    max_reply_chars = getattr(settings, "max_reply_chars", 900)
    result = compose_outbound_reply(
        incoming,
        result,
        max_reply_chars=max_reply_chars,
    )
    if runtime is not None:
        runtime.execution_path = decision.execution_path
        runtime.risk_score = decision.risk.score
        if validation.get("fallback_applied"):
            runtime.register_fallback("factual_validation_failed")
        if judge_report is not None and judge_report.applied:
            runtime.register_fallback("quality_judge_failed")

    response_metadata = result.response_metadata or {}
    outbound_snapshot = {
        "domain": response_metadata.get("domain"),
        "goal": response_metadata.get("goal"),
        "intent": result.intent,
        "response_source": response_metadata.get("response_source"),
        "used_openai_interpreter": bool(response_metadata.get("used_openai_interpreter")),
        "used_openai_responder": bool(response_metadata.get("used_openai_responder")),
        "used_tray": bool(response_metadata.get("used_tray")),
        "fallback_reason": response_metadata.get("fallback_reason"),
        "safety_reason": result.safety_reason,
        "handoff_required": result.handoff_required,
        "reply_chars": len(result.reply_text or ""),
        "reply_preview": redact_text(result.reply_text, max_chars=280),
        "final_order_id": (response_metadata.get("commerce_state") or {}).get("order_id"),
        "final_pending_action": (response_metadata.get("commerce_state") or {}).get(
            "pending_action"
        ),
        "tray_tools": [
            item.get("tool")
            for item in (runtime.tray_calls if runtime else [])
        ],
        "openai_call_types": [
            item.get("call_type")
            for item in (runtime.openai_calls if runtime else [])
        ],
    }
    if runtime is not None:
        runtime.outbound_snapshot = outbound_snapshot
    log_event(
        "agent.response",
        {
            "domain": outbound_snapshot["domain"],
            "goal": outbound_snapshot["goal"],
            "response_source": outbound_snapshot["response_source"],
            "used_openai_interpreter": outbound_snapshot["used_openai_interpreter"],
            "used_openai_responder": outbound_snapshot["used_openai_responder"],
            "used_tray": outbound_snapshot["used_tray"],
            "fallback_reason": outbound_snapshot["fallback_reason"],
            "safety_reason": outbound_snapshot["safety_reason"],
            "handoff_required": outbound_snapshot["handoff_required"],
            "reply_preview": outbound_snapshot["reply_preview"],
            "reply_chars": outbound_snapshot["reply_chars"],
            "tray_tools": outbound_snapshot["tray_tools"],
            "openai_call_types": outbound_snapshot["openai_call_types"],
            "judge_triggered": bool(
                (response_metadata.get("quality_judge") or {}).get("triggered")
            ),
            "factual_validation": response_metadata.get("factual_validation"),
            "commerce_state": summarize_commerce_state(
                response_metadata.get("commerce_state")
            ),
        },
    )
    if runtime is not None:
        outbound_snapshot["openai_api_route"] = runtime.openai_api_route
        outbound_snapshot["openai_api_fallback"] = bool(runtime.openai_api_fallback)
    log_event("turn.end", outbound_snapshot)
    if runtime is not None:
        from app.turn_metrics import build_turn_quality_event

        quality_event = build_turn_quality_event(
            runtime,
            result_metadata={
                **response_metadata,
                "handoff_required": result.handoff_required,
            },
            intent=result.intent,
            model=getattr(settings, "openai_model", None),
        )
        from .rollout import build_rollout_status, observe_turn_for_rollout_alerts

        quality_event["rollout_profile"] = build_rollout_status(settings).get("profile")
        quality_event["openai_api_fallback"] = bool(runtime.openai_api_fallback)
        log_event("turn.quality", quality_event)
        observe_turn_for_rollout_alerts(quality_event, settings=settings)
    if runtime is not None and runtime.openai_api_route:
        log_event(
            "openai.canary.turn",
            {
                "route": runtime.openai_api_route,
                "fallback": bool(runtime.openai_api_fallback),
                "conversation_key_present": runtime.conversation_key != "unresolved",
            },
        )

    if customer_context.get("found") and user_id:
        record_interaction_memory(int(user_id), result.intent, incoming.text)

    if getattr(settings, "agent_memory_proposals_enabled", False) or getattr(
        settings, "agent_instruction_extension_proposals_enabled", False
    ) or getattr(settings, "agent_conversation_summary_enabled", False):
        envelope_payload = (result.response_metadata or {}).get("agent_turn_envelope")
        if isinstance(envelope_payload, dict):
            try:
                from app.memory_models import AgentTurnEnvelope
                from app.memory_service import process_agent_memory_proposals

                envelope = AgentTurnEnvelope.model_validate(envelope_payload)
                memory_result = process_agent_memory_proposals(
                    envelope=envelope,
                    tenant_id=str(
                        getattr(settings, "agent_persona_tenant_id", "newstore")
                    ),
                    conversation_key=(
                        incoming.conversation_id
                        or incoming.sender_key
                        or incoming.sender_phone
                    ),
                    sender_key=incoming.sender_key,
                    inbound=incoming,
                    inbound_id=inbound_id,
                )
                result.response_metadata["memory_processing"] = memory_result.model_dump(
                    mode="json"
                )
            except Exception as exc:
                log_exception("memory.pipeline.error", exc)

    with runtime_stage("enrich_result"):
        enriched = await enrich_agent_result(incoming, result)
        return compose_outbound_reply(
            incoming,
            enriched,
            max_reply_chars=max_reply_chars,
        )
