"""One lifecycle for conversational state, independent of response routing.

Operation facts can be committed before delivery. Conversational references
and memory are committed from the final, delivered answer only.
"""
from __future__ import annotations

from contextlib import contextmanager

from .commerce_context import CommerceConversationState
from .models import AgentResult, IncomingMessage

DIALOGUE_FIELDS = (
    "active_domain", "active_topic", "conversation_goal", "pending_followup",
    "catalog_listing", "last_presented_products", "active_product",
    "previous_active_product", "last_catalog_query", "active_preferences",
    "last_commerce_action", "last_requested_fact", "last_media_product_id",
    "last_media_index", "club_offer_shown", "club_flow_stage",
)

_REVIEW_INPUTS = (
    "cart_id", "cart_session_id", "cart_items", "cart_product_id", "cart_variant_id",
    "cart_quantity", "checkout_draft", "selected_shipping", "selected_payment_option",
    "selected_payment_method", "mercos_payment_condition_id", "mercos_customer_id",
)
_ORDER_CONFIRMATIONS = {"awaiting_order_confirmation", "confirm_purchase"}


def operation_snapshot(state, previous):
    payload = state.model_dump(mode="json")
    old = previous.model_dump(mode="json")
    for key in DIALOGUE_FIELDS:
        if key in old:
            payload[key] = old[key]
    # A newly generated question/review is not a delivered authorization request.
    # Completed operation identifiers and submitted data remain in payload; only
    # transitions that ask the customer to approve something wait for delivery.
    for key in ("pending_action", "pending_commerce_action"):
        if payload[key] != old[key] and payload[key] is not None:
            payload[key] = None
    if payload["pending_action"] is None:
        payload["pending_action_product_ids"] = []

    inputs_changed = any(payload.get(key) != old.get(key) for key in _REVIEW_INPUTS)
    review_changed = state.order_review_version != previous.order_review_version
    previously_delivered = bool(previous.order_review_version
        and previous.order_confirmation_status in {"pending", "confirmed"})
    reusable_review = previously_delivered and not inputs_changed and not review_changed
    if not reusable_review:
        payload["order_confirmation_status"] = "not_ready"
        payload["order_review_version"] = None
        payload["confirmed_order_review_version"] = None
        if payload["pending_action"] in _ORDER_CONFIRMATIONS:
            payload["pending_action"] = None
            payload["pending_action_product_ids"] = []
        if payload["pending_commerce_action"] == "confirm_order":
            payload["pending_commerce_action"] = None
    # A real/ambiguous submission must never resurrect the confirmation that
    # preceded it, even if an identical response is processed again.
    if state.order_id or state.order_creation_ambiguous:
        if payload["pending_action"] in _ORDER_CONFIRMATIONS:
            payload["pending_action"] = None
        if payload["pending_commerce_action"] == "confirm_order":
            payload["pending_commerce_action"] = None

    registration = dict(payload.get("customer_registration") or {})
    old_registration = old.get("customer_registration") or {}
    registration_review_delivered = (
        previous.pending_action == "awaiting_customer_registration_confirmation"
        and old_registration.get("status") == "review"
        and old_registration.get("draft") == registration.get("draft")
    )
    if registration.get("status") == "review" and not registration_review_delivered:
        registration["status"] = "collecting"
        payload["customer_registration"] = registration
        payload["pending_action"] = (
            "awaiting_customer_registration_data"
            if previous.pending_action in {"awaiting_customer_registration_data",
                "awaiting_customer_registration_confirmation"} else None
        )
    if registration.get("status") in {"created", "linked", "created_pending_sync", "unknown",
            "creation_pending", "ambiguous", "lookup_failed", "cancelled", "handoff"}:
        if payload["pending_action"] in {"awaiting_customer_registration_data", "awaiting_customer_registration_confirmation"}:
            payload["pending_action"] = None
    return CommerceConversationState.from_payload(payload)


def finalize_dialogue(result, state, previous):
    """Reconcile pending questions after validators, voice and presentation."""
    from .openai_agent import _trailing_question
    payload = state.model_dump(mode="json")
    meta = result.response_metadata
    rejected = bool(result.handoff_required or result.safety_reason in {
        "factual_validation_failed", "ai_response_composition_failed",
        "consultative_unavailable", "response_critique_failed",
        "response_critique_regenerate_failed",
    })
    if rejected:
        payload = operation_snapshot(state, previous).model_dump(mode="json")
        meta.pop("conversation_summary_delta", None)
    # This is an invitation, never authority to confirm an operation.
    question = None if rejected else _trailing_question(result.reply_text)
    payload["pending_followup"] = {"question": question[:600]} if question else None
    meta["pending_followup"] = payload["pending_followup"]
    return CommerceConversationState.from_payload(payload)


def persist_state(incoming, state, *, persist=None):
    from .customer_identity import resolve_person_key_candidates
    from .db import persist_customer_commerce_session
    (persist or persist_customer_commerce_session)(
        person_keys=resolve_person_key_candidates(sender_key=incoming.sender_key,
            sender_phone=incoming.sender_phone, state=state, workspace_id=incoming.workspace_id),
        workspace_id=incoming.workspace_id, commerce_state=state.model_dump(mode="json"),
        channel=incoming.channel, conversation_id=incoming.conversation_id,
        sender_key=incoming.sender_key, sender_phone=incoming.sender_phone,
    )


@contextmanager
def _ordered_delivery(incoming, settings, response_id):
    """Serialize delivery commits and reject a receipt older than current state.

    A newer inbound is already a watermark, even before its reply is delivered:
    an old review must not rearm a confirmation that the customer has cancelled.
    Keep the lock until both durable writes finish.
    """
    inbound_id = (incoming.raw or {}).get("inbound_id")
    identity = incoming.sender_phone or incoming.sender_key or incoming.conversation_id
    if not getattr(settings, "database_url", None):
        yield True
        return
    if not inbound_id or not identity:
        yield False
        return
    from .db import get_conn
    with get_conn() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                ("conversation-delivery:" + incoming.workspace_id, identity))
            cursor.execute("""
                SELECT 1 FROM public.ai_agent_responses AS response
                LEFT JOIN public.ai_inbound_messages AS inbound
                  ON inbound.id = response.inbound_id AND inbound.workspace_id = response.workspace_id
                WHERE response.workspace_id = %(workspace)s::uuid
                  AND response.provider_send_ok = true
                  AND (response.inbound_id > %(inbound_id)s
                    OR (response.inbound_id = %(inbound_id)s AND response.id > %(response_id)s))
                  AND ((%(sender_phone)s::text IS NOT NULL AND response.sender_phone = %(sender_phone)s::text)
                    OR (%(sender_key)s::text IS NOT NULL AND response.sender_key = %(sender_key)s::text)
                    OR (%(conversation_id)s::text IS NOT NULL AND inbound.conversation_id = %(conversation_id)s::text))
                UNION ALL
                SELECT 1 FROM public.ai_inbound_messages AS newer
                WHERE newer.workspace_id = %(workspace)s::uuid
                  AND newer.id > %(inbound_id)s
                  AND ((%(sender_phone)s::text IS NOT NULL AND newer.sender_phone = %(sender_phone)s::text)
                    OR (%(sender_key)s::text IS NOT NULL AND newer.sender_key = %(sender_key)s::text)
                    OR (%(conversation_id)s::text IS NOT NULL AND newer.conversation_id = %(conversation_id)s::text))
                LIMIT 1
                """, {"workspace": incoming.workspace_id, "inbound_id": int(inbound_id),
                    "response_id": int(response_id or 0), "sender_phone": incoming.sender_phone,
                    "sender_key": incoming.sender_key, "conversation_id": incoming.conversation_id})
            yield cursor.fetchone() is None


def commit_delivered_conversation(data, response_id=None):
    """Called centrally after recording a successful synchronous/queued send."""
    if not data.get("provider_send_ok"):
        return
    metadata = data.get("response_metadata") or {}
    marker = metadata.get("conversation_commit") or {}
    if marker.get("version") != 1:
        return
    from .config import get_settings
    from .memory_scope import restore_state, trusted_workspace
    settings = get_settings()
    workspace = trusted_workspace(settings, data.get("workspace_id"))
    if not workspace:
        return
    snapshot = restore_state(metadata.get("commerce_state"), workspace)
    if not snapshot:
        return
    incoming = IncomingMessage(workspace_id=workspace, channel=data.get("channel") or "unknown",
        conversation_id=marker.get("conversation_id"), sender_key=data.get("sender_key"),
        sender_phone=data.get("sender_phone"), raw={"inbound_id": data.get("inbound_id")})
    with _ordered_delivery(incoming, settings, response_id) as current:
        if not current:
            return
        state = CommerceConversationState.from_payload(snapshot)
        persist_state(incoming, state)
        from .conversation_memory import update_conversation_memory
        result = AgentResult(reply_text=data.get("reply_text") or "", intent=data.get("intent") or "commerce",
            safety_reason=data.get("safety_reason"), handoff_required=bool(data.get("handoff_required")),
            response_metadata={**metadata, "response_id": response_id})
        update_conversation_memory(incoming, result, settings)
