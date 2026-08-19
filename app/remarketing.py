from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from app.brevo_client import send_brevo_reply
from app.config import get_remarketing_touch_hours, get_settings
from app.db import get_conn, to_jsonb
from app.models import IncomingMessage
from app.repository import normalize_phone
from app.tray_adapter_client import TrayAdapterClient


OPT_OUT_PHRASES = {
    "cancelar",
    "nao quero mais mensagens",
    "nao quero receber mensagens",
    "parar",
    "pare",
    "remover",
    "sair",
    "stop",
}


def _fold_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    folded = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", folded).strip().lower()


def is_remarketing_opt_out(text: str | None) -> bool:
    folded = _fold_text(text)
    return folded in OPT_OUT_PHRASES


def remarketing_identity_key(incoming: IncomingMessage) -> str | None:
    if incoming.sender_key:
        return incoming.sender_key
    if incoming.conversation_id:
        return f"conversation:{incoming.conversation_id}"
    if incoming.visitor_id:
        return f"visitor:{incoming.visitor_id}"
    phone = normalize_phone(incoming.sender_phone)
    if phone:
        return f"phone:{phone}"
    return None


def _is_completed(state: dict[str, Any]) -> tuple[bool, str | None]:
    if state.get("purchase_stage") == "payment_confirmed":
        return True, "payment_confirmed"
    if state.get("order_payment_status") == "confirmed":
        return True, "payment_confirmed"
    if state.get("order_has_payment") is True:
        return True, "payment_confirmed"
    return False, None


def _is_commercial_opportunity(
    response_metadata: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    domain = response_metadata.get("domain") or state.get("active_domain")
    goal = response_metadata.get("goal")
    if domain != "commerce" or goal == "after_sales":
        return False
    return state.get("purchase_stage") != "after_sales"


def _remarketing_stage(state: dict[str, Any]) -> str:
    purchase_stage = str(state.get("purchase_stage") or "")
    if (
        purchase_stage == "awaiting_payment"
        or state.get("pending_action") == "awaiting_payment"
        or state.get("order_payment_status") == "pending"
    ):
        return "awaiting_payment"
    if purchase_stage in {
        "checkout_channel_selection",
        "shipping",
        "checkout_ready",
    } or state.get("pending_action") in {
        "awaiting_checkout_data",
        "awaiting_order_confirmation",
        "choose_checkout_channel",
    }:
        return "checkout"
    if state.get("cart_session_id") or purchase_stage == "cart_created":
        return "cart"
    if state.get("active_product") or state.get("last_presented_products"):
        return "product_selection"
    return "commercial_interest"


def _product_name(state: dict[str, Any]) -> str | None:
    active = state.get("active_product")
    if isinstance(active, dict) and active.get("name"):
        return str(active["name"])[:300]
    presented = state.get("last_presented_products")
    if isinstance(presented, list) and presented and isinstance(presented[0], dict):
        name = presented[0].get("name")
        return str(name)[:300] if name else None
    return None


def _safe_link(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:2000] if text.startswith(("https://", "http://")) else None


def sync_remarketing_interaction(
    incoming: IncomingMessage,
    *,
    inbound_id: int | None,
    response_metadata: dict[str, Any] | None,
    handoff_required: bool = False,
) -> None:
    settings = get_settings()
    if not settings.database_url:
        return

    identity_key = remarketing_identity_key(incoming)
    if not identity_key:
        return

    metadata = response_metadata if isinstance(response_metadata, dict) else {}
    state = metadata.get("commerce_state")
    state = state if isinstance(state, dict) else {}
    opted_out = is_remarketing_opt_out(incoming.text)
    completed, completion_reason = _is_completed(state)
    eligible = _is_commercial_opportunity(metadata, state)
    touch_hours = get_remarketing_touch_hours(settings)
    window_hours = max(1, int(settings.remarketing_meta_window_hours))
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(identity_lock)s, 0))",
                {"identity_lock": f"remarketing:{incoming.channel}:{identity_key}"},
            )
            cur.execute(
                """
                INSERT INTO public.ai_remarketing_contacts (
                    channel, identity_key, sender_key, sender_external_id,
                    visitor_id, conversation_id, source_conversation_ref,
                    sender_phone, sender_name, marketing_status,
                    last_customer_message_at, messaging_window_expires_at,
                    opted_out_at
                )
                VALUES (
                    %(channel)s, %(identity_key)s, %(sender_key)s,
                    %(sender_external_id)s, %(visitor_id)s, %(conversation_id)s,
                    %(source_conversation_ref)s, %(sender_phone)s, %(sender_name)s,
                    CASE WHEN %(opted_out)s THEN 'opted_out' ELSE 'eligible' END,
                    %(now)s, %(now)s + make_interval(hours => %(window_hours)s),
                    CASE WHEN %(opted_out)s THEN %(now)s ELSE NULL END
                )
                ON CONFLICT (channel, identity_key) DO UPDATE SET
                    sender_key = COALESCE(EXCLUDED.sender_key, ai_remarketing_contacts.sender_key),
                    sender_external_id = COALESCE(
                        EXCLUDED.sender_external_id,
                        ai_remarketing_contacts.sender_external_id
                    ),
                    visitor_id = COALESCE(EXCLUDED.visitor_id, ai_remarketing_contacts.visitor_id),
                    conversation_id = COALESCE(
                        EXCLUDED.conversation_id,
                        ai_remarketing_contacts.conversation_id
                    ),
                    source_conversation_ref = COALESCE(
                        EXCLUDED.source_conversation_ref,
                        ai_remarketing_contacts.source_conversation_ref
                    ),
                    sender_phone = COALESCE(
                        EXCLUDED.sender_phone,
                        ai_remarketing_contacts.sender_phone
                    ),
                    sender_name = COALESCE(EXCLUDED.sender_name, ai_remarketing_contacts.sender_name),
                    marketing_status = CASE
                        WHEN ai_remarketing_contacts.marketing_status = 'opted_out'
                          OR %(opted_out)s
                        THEN 'opted_out'
                        ELSE 'eligible'
                    END,
                    last_customer_message_at = %(now)s,
                    messaging_window_expires_at =
                        %(now)s + make_interval(hours => %(window_hours)s),
                    opted_out_at = CASE
                        WHEN %(opted_out)s THEN COALESCE(
                            ai_remarketing_contacts.opted_out_at,
                            %(now)s
                        )
                        ELSE ai_remarketing_contacts.opted_out_at
                    END,
                    updated_at = %(now)s
                RETURNING id, marketing_status
                """,
                {
                    "channel": incoming.channel,
                    "identity_key": identity_key,
                    "sender_key": incoming.sender_key,
                    "sender_external_id": incoming.sender_external_id,
                    "visitor_id": incoming.visitor_id,
                    "conversation_id": incoming.conversation_id,
                    "source_conversation_ref": incoming.source_conversation_ref,
                    "sender_phone": normalize_phone(incoming.sender_phone),
                    "sender_name": incoming.sender_name,
                    "opted_out": opted_out,
                    "now": now,
                    "window_hours": window_hours,
                },
            )
            contact = cur.fetchone()
            if not contact:
                return
            contact_id = int(contact["id"])
            marketing_status = str(contact["marketing_status"])

            cur.execute(
                """
                UPDATE public.ai_conversation_statuses
                SET last_customer_message_at = %(now)s,
                    updated_at = %(now)s
                WHERE contact_id = %(contact_id)s
                  AND status = 'active'
                RETURNING id
                """,
                {"contact_id": contact_id, "now": now},
            )
            active_row = cur.fetchone()
            active_id = int(active_row["id"]) if active_row else None

            stop_reason = None
            if marketing_status == "opted_out":
                stop_reason = "customer_opted_out"
            elif handoff_required:
                stop_reason = "human_handoff"
            elif completed:
                stop_reason = completion_reason

            if stop_reason and active_id:
                final_status = "completed" if completed else "cancelled"
                cur.execute(
                    """
                    UPDATE public.ai_conversation_statuses
                    SET status = %(status)s,
                        completed_at = %(now)s,
                        completion_reason = %(reason)s,
                        next_scheduled_at = NULL,
                        updated_at = %(now)s
                    WHERE id = %(active_id)s
                    """,
                    {
                        "status": final_status,
                        "reason": stop_reason,
                        "now": now,
                        "active_id": active_id,
                    },
                )
                cur.execute(
                    """
                    UPDATE public.ai_remarketing_attempts
                    SET status = 'cancelled', updated_at = %(now)s
                    WHERE conversation_status_id = %(active_id)s
                      AND status IN ('pending', 'processing', 'failed')
                    """,
                    {"active_id": active_id, "now": now},
                )
                return
            if stop_reason:
                return

            if marketing_status != "eligible" or not eligible or not touch_hours:
                return

            opportunity = {
                "contact_id": contact_id,
                "stage": _remarketing_stage(state),
                "last_inbound_id": inbound_id,
                "cart_session_id": state.get("cart_session_id"),
                "cart_url": _safe_link(state.get("cart_url")),
                "order_id": state.get("order_id"),
                "payment_url": _safe_link(state.get("order_payment_url")),
                "product_name": _product_name(state),
                "now": now,
                "first_touch": touch_hours[0],
            }
            if active_id:
                opportunity["active_id"] = active_id
                cur.execute(
                    """
                    UPDATE public.ai_conversation_statuses
                    SET stage = %(stage)s,
                        last_inbound_id = %(last_inbound_id)s,
                        cart_session_id = %(cart_session_id)s,
                        cart_url = %(cart_url)s,
                        order_id = %(order_id)s,
                        payment_url = %(payment_url)s,
                        product_name = %(product_name)s,
                        last_customer_message_at = %(now)s,
                        next_scheduled_at = %(now)s
                            + make_interval(hours => %(first_touch)s),
                        updated_at = %(now)s
                    WHERE id = %(active_id)s
                    """,
                    opportunity,
                )
            else:
                cur.execute(
                    """
                    INSERT INTO public.ai_conversation_statuses (
                        contact_id, status, stage, last_inbound_id,
                        cart_session_id, cart_url, order_id, payment_url,
                        product_name, last_customer_message_at, next_scheduled_at
                    )
                    VALUES (
                        %(contact_id)s, 'active', %(stage)s, %(last_inbound_id)s,
                        %(cart_session_id)s, %(cart_url)s, %(order_id)s,
                        %(payment_url)s, %(product_name)s, %(now)s,
                        %(now)s + make_interval(hours => %(first_touch)s)
                    )
                    RETURNING id
                    """,
                    opportunity,
                )
                active_id = int(cur.fetchone()["id"])

            for touch_number, hour in enumerate(touch_hours, start=1):
                cur.execute(
                    """
                    INSERT INTO public.ai_remarketing_attempts (
                        conversation_status_id, touch_number, scheduled_at
                    )
                    VALUES (
                        %(active_id)s,
                        %(touch_number)s,
                        %(now)s + make_interval(hours => %(hour)s)
                    )
                    ON CONFLICT (conversation_status_id, touch_number) DO UPDATE SET
                        scheduled_at = EXCLUDED.scheduled_at,
                        status = CASE
                            WHEN ai_remarketing_attempts.status IN ('sent', 'processing')
                            THEN ai_remarketing_attempts.status
                            ELSE 'pending'
                        END,
                        last_error = CASE
                            WHEN ai_remarketing_attempts.status = 'sent'
                            THEN ai_remarketing_attempts.last_error
                            ELSE NULL
                        END,
                        updated_at = %(now)s
                    """,
                    {
                        "active_id": active_id,
                        "touch_number": touch_number,
                        "hour": hour,
                        "now": now,
                    },
                )
            cur.execute(
                """
                UPDATE public.ai_conversation_statuses
                SET next_scheduled_at = (
                        SELECT min(scheduled_at)
                        FROM public.ai_remarketing_attempts
                        WHERE conversation_status_id = %(active_id)s
                          AND status = 'pending'
                    ),
                    updated_at = %(now)s
                WHERE id = %(active_id)s
                """,
                {"active_id": active_id, "now": now},
            )


def _build_remarketing_message(item: dict[str, Any]) -> str:
    name = str(item.get("sender_name") or "").strip().split(" ")[0]
    greeting = f"Oi, {name}!" if name else "Oi!"
    stage = item.get("stage")
    touch = int(item.get("touch_number") or 1)

    if stage == "awaiting_payment":
        body = "Seu pedido ainda está aguardando pagamento. Posso ajudar a concluir?"
        link = item.get("payment_url")
    elif stage == "checkout":
        body = "Sua compra ficou na etapa de finalização. Posso ajudar com os dados, frete ou pagamento?"
        link = item.get("cart_url")
    elif stage == "cart":
        body = "Você deixou produtos no carrinho. Quer ajuda para finalizar a compra?"
        link = item.get("cart_url")
    elif stage == "product_selection" and item.get("product_name"):
        body = f"Você ainda tem interesse em {item['product_name']}? Posso tirar alguma dúvida."
        link = item.get("cart_url")
    else:
        body = "Vi que sua compra não foi concluída. Posso ajudar a encontrar o produto certo?"
        link = item.get("cart_url")

    if touch == 2:
        body = f"Passando para saber se você ainda precisa de ajuda. {body}"
    elif touch >= 3:
        body = f"Esta é minha última mensagem sobre essa compra. {body}"

    parts = [greeting, body]
    if link:
        parts.append(str(link))
    parts.append("Se não quiser receber estes lembretes, responda SAIR.")
    return "\n\n".join(parts)


def claim_due_remarketing_attempts(limit: int) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.database_url:
        return []
    safe_limit = max(1, min(int(limit), 100))
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_remarketing_attempts
                SET status = 'pending', claimed_at = NULL, updated_at = %(now)s
                WHERE status = 'processing'
                  AND claimed_at < %(now)s - interval '15 minutes'
                """,
                {"now": now},
            )
            cur.execute(
                """
                UPDATE public.ai_remarketing_attempts AS attempt
                SET status = 'expired', updated_at = %(now)s
                FROM public.ai_conversation_statuses AS conversation,
                     public.ai_remarketing_contacts AS contact
                WHERE attempt.conversation_status_id = conversation.id
                  AND conversation.contact_id = contact.id
                  AND conversation.status = 'active'
                  AND attempt.status IN ('pending', 'processing', 'failed')
                  AND contact.messaging_window_expires_at <= %(now)s
                """,
                {"now": now},
            )
            cur.execute(
                """
                UPDATE public.ai_conversation_statuses AS conversation
                SET status = 'expired',
                    completed_at = %(now)s,
                    completion_reason = 'messaging_window_expired',
                    next_scheduled_at = NULL,
                    updated_at = %(now)s
                FROM public.ai_remarketing_contacts AS contact
                WHERE conversation.contact_id = contact.id
                  AND conversation.status = 'active'
                  AND contact.messaging_window_expires_at <= %(now)s
                """,
                {"now": now},
            )
            cur.execute(
                """
                WITH candidates AS (
                    SELECT
                        attempt.id,
                        attempt.touch_number,
                        conversation.id AS conversation_status_id,
                        conversation.stage,
                        conversation.cart_session_id,
                        conversation.cart_url,
                        conversation.order_id,
                        conversation.payment_url,
                        conversation.product_name,
                        contact.channel,
                        contact.sender_key,
                        contact.sender_external_id,
                        contact.visitor_id,
                        contact.conversation_id,
                        contact.source_conversation_ref,
                        contact.sender_phone,
                        contact.sender_name
                    FROM public.ai_remarketing_attempts AS attempt
                    JOIN public.ai_conversation_statuses AS conversation
                      ON conversation.id = attempt.conversation_status_id
                    JOIN public.ai_remarketing_contacts AS contact
                      ON contact.id = conversation.contact_id
                    WHERE attempt.status = 'pending'
                      AND attempt.scheduled_at <= %(now)s
                      AND conversation.status = 'active'
                      AND contact.marketing_status = 'eligible'
                      AND contact.messaging_window_expires_at > %(now)s
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.ai_remarketing_attempts AS earlier
                          WHERE earlier.conversation_status_id =
                              attempt.conversation_status_id
                            AND earlier.touch_number < attempt.touch_number
                            AND earlier.status IN ('pending', 'processing')
                      )
                    ORDER BY attempt.scheduled_at, attempt.id
                    FOR UPDATE OF attempt SKIP LOCKED
                    LIMIT %(limit)s
                ),
                claimed AS (
                    UPDATE public.ai_remarketing_attempts AS attempt
                    SET status = 'processing',
                        attempt_count = attempt.attempt_count + 1,
                        claimed_at = %(now)s,
                        updated_at = %(now)s
                    FROM candidates
                    WHERE attempt.id = candidates.id
                    RETURNING attempt.id
                )
                SELECT candidates.*
                FROM candidates
                JOIN claimed ON claimed.id = candidates.id
                ORDER BY candidates.id
                """,
                {"now": now, "limit": safe_limit},
            )
            return list(cur.fetchall() or [])


def complete_paid_remarketing(
    conversation_status_id: int,
    attempt_id: int,
) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_conversation_statuses
                SET status = 'completed',
                    completed_at = %(now)s,
                    completion_reason = 'payment_confirmed_by_tray',
                    next_scheduled_at = NULL,
                    updated_at = %(now)s
                WHERE id = %(conversation_status_id)s
                  AND status = 'active'
                """,
                {"conversation_status_id": conversation_status_id, "now": now},
            )
            cur.execute(
                """
                UPDATE public.ai_remarketing_attempts
                SET status = 'cancelled',
                    last_error = CASE
                        WHEN id = %(attempt_id)s THEN 'payment_already_confirmed'
                        ELSE last_error
                    END,
                    updated_at = %(now)s
                WHERE conversation_status_id = %(conversation_status_id)s
                  AND status IN ('pending', 'processing', 'failed')
                """,
                {
                    "conversation_status_id": conversation_status_id,
                    "attempt_id": attempt_id,
                    "now": now,
                },
            )


def finish_remarketing_attempt(
    attempt_id: int,
    *,
    message_text: str,
    send_ok: bool,
    provider_response: dict[str, Any],
    error: str | None,
) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.ai_remarketing_attempts AS attempt
                SET status = CASE
                        WHEN %(send_ok)s THEN 'sent'
                        WHEN attempt.attempt_count < 3
                          AND contact.messaging_window_expires_at
                              > %(now)s + interval '15 minutes'
                        THEN 'pending'
                        ELSE 'failed'
                    END,
                    scheduled_at = CASE
                        WHEN NOT %(send_ok)s
                          AND attempt.attempt_count < 3
                          AND contact.messaging_window_expires_at
                              > %(now)s + interval '15 minutes'
                        THEN %(now)s + interval '15 minutes'
                        ELSE attempt.scheduled_at
                    END,
                    sent_at = CASE WHEN %(send_ok)s THEN %(now)s ELSE NULL END,
                    message_text = %(message_text)s,
                    provider_send_ok = %(send_ok)s,
                    provider_response = %(provider_response)s,
                    last_error = %(error)s,
                    updated_at = %(now)s
                FROM public.ai_conversation_statuses AS conversation,
                     public.ai_remarketing_contacts AS contact
                WHERE attempt.id = %(attempt_id)s
                  AND conversation.id = attempt.conversation_status_id
                  AND contact.id = conversation.contact_id
                RETURNING conversation.id
                """,
                {
                    "attempt_id": attempt_id,
                    "message_text": message_text,
                    "send_ok": send_ok,
                    "provider_response": to_jsonb(provider_response),
                    "error": error,
                    "now": now,
                },
            )
            row = cur.fetchone()
            if not row:
                return
            cur.execute(
                """
                UPDATE public.ai_conversation_statuses
                SET next_scheduled_at = (
                        SELECT min(scheduled_at)
                        FROM public.ai_remarketing_attempts
                        WHERE conversation_status_id = %(conversation_id)s
                          AND status = 'pending'
                    ),
                    updated_at = %(now)s
                WHERE id = %(conversation_id)s
                """,
                {"conversation_id": int(row["id"]), "now": now},
            )


async def run_remarketing_batch(limit: int | None = None) -> dict[str, int]:
    settings = get_settings()
    if not settings.remarketing_enabled:
        return {"claimed": 0, "sent": 0, "failed": 0}

    items = claim_due_remarketing_attempts(limit or settings.remarketing_batch_size)
    sent = 0
    failed = 0
    for item in items:
        message_text = ""
        order_id = str(item.get("order_id") or "").strip()
        cart_session_id = str(item.get("cart_session_id") or "").strip()
        if order_id or cart_session_id:
            try:
                tray = TrayAdapterClient()
                if not order_id:
                    order_lookup = await tray.list_orders(session_id=cart_session_id)
                    if (
                        not isinstance(order_lookup, dict)
                        or order_lookup.get("success") is False
                        or order_lookup.get("error")
                    ):
                        raise RuntimeError("cart_order_verification_failed")
                    orders = order_lookup.get("orders")
                    matching_order = next(
                        (
                            order
                            for order in orders
                            if isinstance(order, dict)
                            and (order.get("order_id") or order.get("id"))
                        ),
                        None,
                    ) if isinstance(orders, list) else None
                    if matching_order:
                        order_id = str(
                            matching_order.get("order_id")
                            or matching_order.get("id")
                            or ""
                        ).strip()

                if not order_id:
                    raise LookupError("no_order_for_cart")

                payment_result = await tray.get_order_payment(order_id)
                payment = (
                    payment_result.get("payment")
                    if isinstance(payment_result, dict)
                    else None
                )
                if isinstance(payment, dict) and payment.get("has_payment") is True:
                    complete_paid_remarketing(
                        int(item["conversation_status_id"]),
                        int(item["id"]),
                    )
                    continue
                if (
                    not isinstance(payment_result, dict)
                    or payment_result.get("success") is False
                    or payment_result.get("error")
                    or not isinstance(payment, dict)
                    or payment.get("has_payment") is not False
                ):
                    raise RuntimeError("order_payment_verification_failed")
                item["stage"] = "awaiting_payment"
                item["payment_url"] = _safe_link(payment.get("payment_url"))
            except LookupError:
                pass
            except Exception:
                finish_remarketing_attempt(
                    int(item["id"]),
                    message_text=message_text,
                    send_ok=False,
                    provider_response={"verification": "failed"},
                    error="order_payment_verification_failed",
                )
                failed += 1
                continue

        message_text = _build_remarketing_message(item)
        incoming = IncomingMessage(
            provider="brevo",
            channel=item["channel"],
            sender_key=item.get("sender_key"),
            sender_external_id=item.get("sender_external_id"),
            visitor_id=item.get("visitor_id"),
            conversation_id=item.get("conversation_id"),
            source_conversation_ref=item.get("source_conversation_ref"),
            sender_phone=item.get("sender_phone"),
            sender_name=item.get("sender_name"),
            text="",
        )
        try:
            result = await send_brevo_reply(incoming, message_text)
            send_ok = bool(result.ok)
            provider_response = result.model_dump(mode="json")
            error = result.error
        except Exception as exc:
            send_ok = False
            provider_response = {"error_type": type(exc).__name__}
            error = "unexpected_send_error"

        finish_remarketing_attempt(
            int(item["id"]),
            message_text=message_text,
            send_ok=send_ok,
            provider_response=provider_response,
            error=error,
        )
        if send_ok:
            sent += 1
        else:
            failed += 1

    return {"claimed": len(items), "sent": sent, "failed": failed}
