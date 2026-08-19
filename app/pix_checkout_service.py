"""WhatsApp direct PIX checkout: generate MP PIX after order confirmation."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .config import Settings, get_settings
from .commerce_context import CommerceConversationState
from .mercadopago_client import MercadoPagoError
from .models import AgentResult
from .order_service import _current_order_facts
from .pix_payment_service import create_and_persist_pix_payment, refresh_pix_payment_status
from .pix_settlement import brl_to_cents, settle_approved_pix_payment

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def pix_direct_available(settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    return bool(cfg.pix_direct_enabled and cfg.resolved_mp_access_token())


def should_use_direct_pix(
    state: CommerceConversationState,
    *,
    settings: Settings | None = None,
) -> bool:
    if not pix_direct_available(settings):
        return False
    if state.checkout_channel_preference != "whatsapp":
        return False
    method = state.selected_payment_method or state.payment_method_preference
    if method != "pix":
        return False
    # Already have a Tray order — use hosted/order payment path.
    if state.order_id:
        return False
    return True


def _pix_reply(copy_paste: str, amount_label: str | None) -> str:
    amount_bit = f" Valor: R$ {amount_label}." if amount_label else ""
    return (
        f"Segue o PIX para pagamento.{amount_bit}\n"
        f"Copia e cola:\n{copy_paste}\n"
        "Assim que o pagamento for confirmado, eu crio o pedido automaticamente. "
        "Se quiser, diga \"já paguei\" depois de pagar."
    )


async def generate_direct_pix_checkout(
    *,
    state: CommerceConversationState,
    execute: ToolExecutor,
    settings: Settings | None = None,
    conversation_id: str | None = None,
    sender_key: str | None = None,
    sender_phone: str | None = None,
    channel: str | None = None,
) -> AgentResult:
    cfg = settings or get_settings()
    if not should_use_direct_pix(state, settings=cfg):
        return AgentResult(
            reply_text="O PIX direto no chat não está disponível neste momento.",
            intent="commerce",
            safety_reason="pix_direct_unavailable",
            commercial_data={"success": False, "stage": "pix_direct"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    if (
        state.order_confirmation_status != "confirmed"
        or not state.order_review_version
        or state.confirmed_order_review_version != state.order_review_version
    ):
        return AgentResult(
            reply_text="A geração do PIX está bloqueada sem confirmação do resumo atual.",
            intent="commerce",
            safety_reason="order_confirmation_required",
            commercial_data={"success": False, "stage": "pix_direct"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    # Reuse pending PIX for the same review version when still open.
    if (
        state.pix_payment_id
        and state.pix_copy_paste_code
        and state.pix_payment_status in {None, "pending", "in_process"}
        and state.pix_order_review_version == state.confirmed_order_review_version
    ):
        amount_label = state.pix_amount_label
        return AgentResult(
            reply_text=_pix_reply(state.pix_copy_paste_code, amount_label),
            intent="commerce",
            commercial_data={
                "success": True,
                "stage": "pix_direct",
                "pix": {
                    "payment_id": state.pix_payment_id,
                    "status": state.pix_payment_status or "pending",
                    "copy_paste_code": state.pix_copy_paste_code,
                    "reused": True,
                },
            },
            response_metadata={
                "domain": "commerce",
                "purchase_stage": "awaiting_payment",
                "pending_action": "awaiting_payment",
                "pending_action_product_ids": [],
                "payment_state": {
                    "order_payment_status": "pending",
                    "order_has_payment": False,
                    "order_payment_method": "pix",
                    "order_payment_type": "pix_direct",
                    "order_payment_url": None,
                },
                "pix_state": {
                    "pix_payment_id": state.pix_payment_id,
                    "pix_payment_status": state.pix_payment_status or "pending",
                    "pix_copy_paste_code": state.pix_copy_paste_code,
                    "pix_amount_label": amount_label,
                    "pix_order_review_version": state.pix_order_review_version,
                },
                "factual_fallback_text": _pix_reply(
                    state.pix_copy_paste_code, amount_label
                ),
                "used_tray": True,
            },
        )

    facts, missing = await _current_order_facts(state, execute)
    if facts is None:
        return AgentResult(
            reply_text="Os fatos do pedido mudaram; não posso gerar o PIX agora.",
            intent="commerce",
            safety_reason="order_confirmation_stale",
            commercial_data={
                "success": False,
                "stage": "pix_direct",
                "missing_fields": missing,
            },
            response_metadata={"domain": "commerce", "used_tray": True},
        )
    if facts["version"] != state.confirmed_order_review_version:
        return AgentResult(
            reply_text="Os fatos do pedido mudaram; confirme o novo resumo antes do PIX.",
            intent="commerce",
            safety_reason="order_confirmation_stale",
            commercial_data=facts.get("summary") or {},
            response_metadata={
                "domain": "commerce",
                "order_state": {
                    "order_confirmation_status": "pending",
                    "order_review_version": facts["version"],
                    "confirmed_order_review_version": None,
                },
                "purchase_stage": "order_review",
                "pending_action": "awaiting_order_confirmation",
                "used_tray": True,
            },
        )

    summary = facts["summary"] if isinstance(facts.get("summary"), dict) else {}
    display_total = summary.get("display_total")
    expected_cents = brl_to_cents(display_total)
    if expected_cents is None or expected_cents <= 0:
        return AgentResult(
            reply_text="Não consegui calcular o valor do PIX com segurança.",
            intent="commerce",
            safety_reason="pix_amount_unavailable",
            commercial_data={"success": False, "stage": "pix_direct"},
            response_metadata={"domain": "commerce", "used_tray": True},
        )

    amount_brl = expected_cents / 100.0
    payer_email = (
        (state.checkout_draft.customer.email or "").strip()
        or "comprador@example.com"
    )
    description = f"Pedido New Store WhatsApp - {state.cart_session_id or 'carrinho'}"
    checkout_snapshot = {
        "expected_amount_cents": expected_cents,
        "display_total": str(display_total),
        "order_payload": facts["payload"],
        "order_summary": summary,
        "order_review_version": facts["version"],
    }

    try:
        created, _row_id = await create_and_persist_pix_payment(
            transaction_amount=amount_brl,
            description=description,
            payer_email=payer_email,
            external_reference=str(state.cart_session_id or facts["version"][:16]),
            metadata={
                "source": "ns_agent_whatsapp_pix",
                "cart_session_id": state.cart_session_id,
                "order_review_version": facts["version"],
            },
            conversation_id=conversation_id,
            sender_key=sender_key,
            sender_phone=sender_phone,
            channel=channel or "whatsapp",
            cart_session_id=state.cart_session_id,
            checkout_snapshot=checkout_snapshot,
            settings=cfg,
        )
    except MercadoPagoError as exc:
        print("[sales.pix.create.failed]", {
            "code": exc.code,
            "status_code": exc.status_code,
        })
        return AgentResult(
            reply_text="Não consegui gerar o PIX agora. Tente novamente em instantes.",
            intent="commerce",
            safety_reason="pix_create_failed",
            commercial_data={
                "success": False,
                "stage": "pix_direct",
                "error": exc.code,
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    # Guard: never proceed if MP charged a different amount than the order total.
    created_cents = brl_to_cents(created.transaction_amount)
    if created_cents is None:
        created_cents = expected_cents
    if created_cents != expected_cents:
        print("[sales.pix.create.amount_mismatch]", {
            "expected_cents": expected_cents,
            "created_cents": created_cents,
            "payment_id": created.payment_id,
        })
        return AgentResult(
            reply_text=(
                "O PIX gerado não bateu com o valor do pedido; "
                "não vou seguir com essa cobrança."
            ),
            intent="commerce",
            safety_reason="pix_amount_mismatch",
            commercial_data={
                "success": False,
                "stage": "pix_direct",
                "expected_amount_cents": expected_cents,
                "pix_amount_cents": created_cents,
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    copy_paste = created.copy_paste_code or created.qr_code
    if not copy_paste:
        return AgentResult(
            reply_text="O PIX foi criado, mas o código copia e cola não veio completo.",
            intent="commerce",
            safety_reason="pix_code_missing",
            commercial_data={
                "success": False,
                "stage": "pix_direct",
                "payment_id": created.payment_id,
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    amount_label = str(display_total)
    reply = _pix_reply(copy_paste, amount_label)
    print("[sales.pix.create.ok]", {
        "payment_id": created.payment_id,
        "amount_cents": expected_cents,
    })
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        commercial_data={
            "success": True,
            "stage": "pix_direct",
            "pix": {
                "payment_id": created.payment_id,
                "status": created.status,
                "copy_paste_code": copy_paste,
                "qr_code": created.qr_code,
                "amount": amount_label,
                "expires_in_seconds": created.expires_in_seconds,
                "reused": False,
            },
            "order_summary": summary,
        },
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "awaiting_payment",
            "pending_action": "awaiting_payment",
            "pending_action_product_ids": [],
            "payment_state": {
                "order_payment_status": "pending",
                "order_has_payment": False,
                "order_payment_method": "pix",
                "order_payment_type": "pix_direct",
                "order_payment_url": None,
            },
            "pix_state": {
                "pix_payment_id": created.payment_id,
                "pix_payment_status": created.status,
                "pix_copy_paste_code": copy_paste,
                "pix_amount_label": amount_label,
                "pix_order_review_version": facts["version"],
            },
            "factual_fallback_text": reply,
            "used_tray": True,
        },
    )


async def refresh_direct_pix_checkout(
    *,
    state: CommerceConversationState,
    settings: Settings | None = None,
) -> AgentResult:
    """Handle 'já paguei' for direct PIX (before Tray order exists)."""
    cfg = settings or get_settings()
    payment_id = state.pix_payment_id
    if not payment_id:
        return AgentResult(
            reply_text="Não há um PIX pendente nesta conversa.",
            intent="commerce",
            safety_reason="pix_pending_missing",
            commercial_data={"success": False, "stage": "pix_direct"},
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    try:
        refreshed = await refresh_pix_payment_status(payment_id, settings=cfg)
    except MercadoPagoError as exc:
        return AgentResult(
            reply_text="Ainda não consegui confirmar o pagamento do PIX.",
            intent="commerce",
            safety_reason="pix_status_unavailable",
            commercial_data={
                "success": False,
                "stage": "pix_direct",
                "error": exc.code,
            },
            response_metadata={"domain": "commerce", "used_tray": False},
        )

    status = str(refreshed.get("status") or "pending").lower()
    settlement = None
    if status == "approved":
        settlement = await settle_approved_pix_payment(
            payment_id,
            mp_payload=refreshed.get("raw")
            if isinstance(refreshed.get("raw"), dict)
            else None,
        )

    if status == "approved" and settlement and settlement.get("ok"):
        order_id = settlement.get("tray_order_id")
        reply = (
            f"Pagamento confirmado e pedido criado: {order_id}."
            if order_id
            else "Pagamento confirmado."
        )
        return AgentResult(
            reply_text=reply,
            intent="commerce",
            commercial_data={
                "success": True,
                "stage": "pix_direct",
                "pix": {"payment_id": payment_id, "status": status},
                "order_id": order_id,
                "settlement": settlement,
            },
            response_metadata={
                "domain": "commerce",
                "purchase_stage": "order_created" if order_id else "awaiting_payment",
                "clear_pending_action": bool(order_id),
                "payment_state": {
                    "order_payment_status": "confirmed",
                    "order_has_payment": True,
                    "order_payment_method": "pix",
                    "order_payment_type": "pix_direct",
                },
                "pix_state": {
                    "pix_payment_id": payment_id,
                    "pix_payment_status": status,
                    "pix_copy_paste_code": state.pix_copy_paste_code,
                    "pix_amount_label": state.pix_amount_label,
                    "pix_order_review_version": state.pix_order_review_version,
                },
                **(
                    {
                        "order_state": {
                            "order_id": str(order_id),
                            "order_session_id": state.cart_session_id,
                            "order_confirmation_status": "not_ready",
                            "order_review_version": None,
                            "confirmed_order_review_version": None,
                            "order_creation_ambiguous": False,
                        }
                    }
                    if order_id
                    else {}
                ),
                "factual_fallback_text": reply,
                "used_tray": True,
            },
        )

    if status == "approved" and settlement and not settlement.get("ok"):
        reason = settlement.get("reason") or "settle_failed"
        return AgentResult(
            reply_text=(
                "Recebi a confirmação do PIX, mas ainda não consegui criar o pedido. "
                "Vou precisar de um momento ou de suporte."
            ),
            intent="commerce",
            safety_reason="pix_settle_failed",
            commercial_data={
                "success": False,
                "stage": "pix_direct",
                "settlement_reason": reason,
            },
            response_metadata={
                "domain": "commerce",
                "pix_state": {
                    "pix_payment_id": payment_id,
                    "pix_payment_status": status,
                    "pix_copy_paste_code": state.pix_copy_paste_code,
                    "pix_amount_label": state.pix_amount_label,
                    "pix_order_review_version": state.pix_order_review_version,
                },
                "used_tray": True,
            },
        )

    reply = (
        "Ainda não identifiquei a confirmação do PIX. "
        "Se já pagou, aguarde um instante e tente de novo."
    )
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        commercial_data={
            "success": True,
            "stage": "pix_direct",
            "pix": {"payment_id": payment_id, "status": status, "pending": True},
        },
        response_metadata={
            "domain": "commerce",
            "purchase_stage": "awaiting_payment",
            "pending_action": "awaiting_payment",
            "payment_state": {
                "order_payment_status": "pending",
                "order_has_payment": False,
                "order_payment_method": "pix",
                "order_payment_type": "pix_direct",
            },
            "pix_state": {
                "pix_payment_id": payment_id,
                "pix_payment_status": status,
                "pix_copy_paste_code": state.pix_copy_paste_code,
                "pix_amount_label": state.pix_amount_label,
                "pix_order_review_version": state.pix_order_review_version,
            },
            "factual_fallback_text": reply,
            "used_tray": False,
        },
    )
