"""Hourly attendance analysis → learning insights + instruction extensions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import get_settings
from .db import get_conn, get_returning_id, to_jsonb
from .greeting_policy import is_generic_greeting_reply
from .guardrails import detect_trade_in_or_appraisal_request
from .instruction_extension_repository import create_extension_proposal


_EMPTY_CATALOG_RE = re.compile(
    r"n[aã]o encontrei (esse produto|op[cç][oõ]es)",
    flags=re.IGNORECASE,
)
_TRADE_DENIAL_RE = re.compile(
    r"n[aã]o compramos|apenas vendemos|s[oó] vendemos produtos novos",
    flags=re.IGNORECASE,
)


def _fold(text: str) -> str:
    return " ".join((text or "").lower().split())


def _insight_key(category: str, title: str) -> str:
    digest = hashlib.sha256(f"{category}|{title}".encode("utf-8")).hexdigest()[:16]
    return f"{category}:{digest}"


def fetch_recent_attendances(
    *,
    tenant_id: str,
    lookback_hours: int = 2,
    limit: int = 120,
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, lookback_hours))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    response.id AS response_id,
                    response.inbound_id,
                    response.reply_text AS agent_reply,
                    response.intent,
                    response.handoff_required,
                    response.safety_reason,
                    response.response_metadata,
                    response.created_at AS response_created_at,
                    response.sender_key,
                    inbound.text AS customer_text,
                    inbound.channel,
                    inbound.conversation_id,
                    inbound.sender_phone
                FROM public.ai_agent_responses AS response
                LEFT JOIN public.ai_inbound_messages AS inbound
                  ON inbound.id = response.inbound_id
                WHERE response.created_at >= %s
                ORDER BY response.created_at DESC
                LIMIT %s
                """,
                (since, limit),
            )
            rows = list(cur.fetchall() or [])
    # tenant_id is reserved for multi-tenant filtering when column exists.
    _ = tenant_id
    return rows


def classify_attendance(row: dict[str, Any]) -> dict[str, Any]:
    customer = str(row.get("customer_text") or "")
    reply = str(row.get("agent_reply") or "")
    metadata = row.get("response_metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    failure_codes: list[str] = []
    outcome = "success"

    if row.get("handoff_required"):
        outcome = "handoff"
    if _EMPTY_CATALOG_RE.search(reply):
        failure_codes.append("empty_catalog")
        outcome = "empty_catalog"
        if any(
            token in customer.lower()
            for token in ("feminino", "masculino", "até", "ate", "reais")
        ):
            failure_codes.append("preference_misread")
    if is_generic_greeting_reply(reply) and is_generic_greeting_reply(customer):
        # Soft signal only — greeting answering greeting is fine once.
        pass
    if detect_trade_in_or_appraisal_request(customer):
        if _TRADE_DENIAL_RE.search(reply) or not row.get("handoff_required"):
            failure_codes.append("trade_in_policy_miss")
            outcome = "policy_miss"
    critique = metadata.get("response_critique") if isinstance(metadata, dict) else None
    if isinstance(critique, dict) and critique.get("approved") is False:
        failure_codes.append("critique_failed")
        outcome = "failure"
    if failure_codes and outcome == "success":
        outcome = "failure"
    return {
        "outcome": outcome,
        "failure_codes": failure_codes,
        "signals": {
            "intent": row.get("intent"),
            "safety_reason": row.get("safety_reason"),
            "channel": row.get("channel"),
        },
    }


def persist_attendance_review(
    *,
    tenant_id: str,
    row: dict[str, Any],
    classification: dict[str, Any],
) -> int | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_attendance_reviews (
                    tenant_id, conversation_key, sender_key,
                    inbound_id, response_id, channel,
                    customer_text, agent_reply, outcome,
                    failure_codes, signals
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    row.get("conversation_id"),
                    row.get("sender_key"),
                    row.get("inbound_id"),
                    row.get("response_id"),
                    row.get("channel"),
                    row.get("customer_text"),
                    row.get("agent_reply"),
                    classification["outcome"],
                    to_jsonb(classification["failure_codes"]),
                    to_jsonb(classification["signals"]),
                ),
            )
            return get_returning_id(cur.fetchone())


def upsert_learning_insight(
    *,
    tenant_id: str,
    category: str,
    title: str,
    insight_text: str,
    evidence_count: int,
    confidence: float,
    importance: float,
    source_review_ids: list[int],
    metadata: dict[str, Any] | None = None,
) -> int | None:
    key = _insight_key(category, title)
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.ai_learning_insights (
                    tenant_id, insight_key, category, title, insight_text,
                    evidence_count, confidence, importance, status,
                    source_review_ids, metadata, first_seen_at, last_seen_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, 'pending_review',
                    %s, %s, %s, %s
                )
                ON CONFLICT (tenant_id, insight_key) WHERE status = 'pending_review'
                DO UPDATE SET
                    evidence_count = public.ai_learning_insights.evidence_count + EXCLUDED.evidence_count,
                    confidence = GREATEST(
                        public.ai_learning_insights.confidence,
                        EXCLUDED.confidence
                    ),
                    importance = GREATEST(
                        public.ai_learning_insights.importance,
                        EXCLUDED.importance
                    ),
                    last_seen_at = EXCLUDED.last_seen_at,
                    source_review_ids = public.ai_learning_insights.source_review_ids
                        || EXCLUDED.source_review_ids,
                    insight_text = EXCLUDED.insight_text,
                    updated_at = EXCLUDED.last_seen_at
                RETURNING id
                """,
                (
                    tenant_id,
                    key,
                    category,
                    title,
                    insight_text,
                    evidence_count,
                    confidence,
                    importance,
                    to_jsonb(source_review_ids),
                    to_jsonb(metadata or {}),
                    now,
                    now,
                ),
            )
            return get_returning_id(cur.fetchone())


def _aggregate_failures(reviews: list[dict[str, Any]]) -> dict[str, list[int]]:
    buckets: dict[str, list[int]] = {}
    for review in reviews:
        review_id = review.get("id")
        for code in review.get("failure_codes") or []:
            buckets.setdefault(str(code), []).append(int(review_id))
    return buckets


_INSIGHT_TEMPLATES: dict[str, tuple[str, str, str, str]] = {
    # category, title, instruction, importance-ish text
    "preference_misread": (
        "retrieval",
        "Preferências de gênero/orçamento não viram busca útil",
        (
            "Quando o cliente complementar 'quero um relógio' com gênero "
            "(feminino/masculino) e/ou orçamento (até X reais), trate como "
            "recomendação de catálogo — nunca como modelo exato. Use recipient/"
            "attributes para gênero e budget_max para o valor."
        ),
        "persona",
    ),
    "empty_catalog": (
        "retrieval",
        "Respostas vazias de catálogo em descoberta",
        (
            "Em buscas por preferência (estilo, gênero, orçamento), se a consulta "
            "exata falhar, refaça com categoria + filtros semânticos antes de "
            "dizer que não encontrou o produto."
        ),
        "knowledge",
    ),
    "trade_in_policy_miss": (
        "handoff",
        "Política de avaliação/troca/compra de usados",
        (
            "A New Store avalia, troca e compra relógios. Pedidos de seminovo, "
            "avaliação ou troca devem ir para atendente humano — nunca negar a política."
        ),
        "policy",
    ),
    "critique_failed": (
        "persona",
        "Respostas reprovadas pelo juiz",
        (
            "Antes de enviar, confira se a resposta respeita preferências do cliente, "
            "não inventa fatos comerciais e não repete saudação genérica."
        ),
        "persona",
    ),
}


def promote_insights_to_extensions(
    *,
    tenant_id: str,
    insight_id: int,
    category: str,
    insight_text: str,
    confidence: float,
    importance: float,
) -> int | None:
    """Create an instruction extension from a learning insight (optionally activate).

    Etapa 9: default is pending_review only. Activation requires
    AGENT_LEARNING_AUTO_ACTIVATE=true (rollback/emergency) or admin approve.
    """
    settings = get_settings()
    extension_key = f"learning:{category}:{insight_id}"
    try:
        created = create_extension_proposal(
            tenant_id=tenant_id,
            extension_key=extension_key,
            instruction_text=insight_text,
            category=category,
            scope="tenant",
            source="model_proposal",
            importance=importance,
            confidence=confidence,
            metadata={"source": "attendance_learning", "insight_id": insight_id},
        )
    except Exception as exc:
        print("[attendance.learning.extension_error]", {
            "error_type": type(exc).__name__,
            "error": str(exc)[:160],
        })
        return None
    extension_id = created.get("id") if isinstance(created, dict) else None
    # Etapa 13 / v6: cron never auto-approves. Admin API only.
    activated = False
    if extension_id and bool(
        getattr(settings, "agent_learning_auto_activate", False)
    ):
        print(
            "[attendance.learning.activate_skipped]",
            {
                "reason": "cron_cannot_approve",
                "extension_id": extension_id,
                "hint": "use admin approve endpoint",
            },
        )
    if extension_id:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if activated:
                    cur.execute(
                        """
                        UPDATE public.ai_learning_insights
                        SET applied_extension_id = %s,
                            status = 'applied',
                            reviewed_at = now(),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (extension_id, insight_id),
                    )
                else:
                    # Link pending extension; keep insight pending_review for humans.
                    cur.execute(
                        """
                        UPDATE public.ai_learning_insights
                        SET applied_extension_id = %s,
                            updated_at = now()
                        WHERE id = %s
                          AND status = 'pending_review'
                        """,
                        (extension_id, insight_id),
                    )
                print("[attendance.learning.promote]", {
                    "insight_id": insight_id,
                    "extension_id": extension_id,
                    "activated": activated,
                })
    return extension_id


async def run_attendance_learning_batch(
    *,
    lookback_hours: int | None = None,
    limit: int | None = None,
    auto_promote: bool | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    tenant_id = str(getattr(settings, "agent_persona_tenant_id", "newstore") or "newstore")
    hours = lookback_hours or int(
        getattr(settings, "agent_learning_lookback_hours", 2) or 2
    )
    row_limit = limit or int(getattr(settings, "agent_learning_batch_limit", 120) or 120)
    # Etapa 9: promote off by default; explicit arg overrides for tests/ops.
    if auto_promote is None:
        auto_apply = bool(getattr(settings, "agent_learning_auto_promote", False))
    else:
        auto_apply = bool(auto_promote)

    rows = fetch_recent_attendances(
        tenant_id=tenant_id,
        lookback_hours=hours,
        limit=row_limit,
    )
    reviews: list[dict[str, Any]] = []
    for row in rows:
        classification = classify_attendance(row)
        try:
            review_id = persist_attendance_review(
                tenant_id=tenant_id,
                row=row,
                classification=classification,
            )
        except Exception as exc:
            print("[attendance.learning.review_error]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:160],
            })
            continue
        if review_id is None:
            continue
        reviews.append({
            "id": review_id,
            "failure_codes": classification["failure_codes"],
            "outcome": classification["outcome"],
        })

    buckets = _aggregate_failures(reviews)
    insights_created = 0
    extensions_created = 0
    for code, review_ids in buckets.items():
        template = _INSIGHT_TEMPLATES.get(code)
        if not template:
            continue
        category, title, insight_text, extension_category = template
        evidence = len(review_ids)
        confidence = min(0.95, 0.45 + 0.1 * evidence)
        importance = min(0.95, 0.5 + 0.08 * evidence)
        try:
            insight_id = upsert_learning_insight(
                tenant_id=tenant_id,
                category=category,
                title=title,
                insight_text=insight_text,
                evidence_count=evidence,
                confidence=confidence,
                importance=importance,
                source_review_ids=review_ids[:40],
                metadata={"failure_code": code},
            )
        except Exception as exc:
            print("[attendance.learning.insight_error]", {
                "error_type": type(exc).__name__,
                "error": str(exc)[:160],
                "code": code,
            })
            continue
        if insight_id is None:
            continue
        insights_created += 1
        if auto_apply and evidence >= 2 and confidence >= 0.6:
            ext_id = promote_insights_to_extensions(
                tenant_id=tenant_id,
                insight_id=insight_id,
                category=extension_category,
                insight_text=insight_text,
                confidence=confidence,
                importance=importance,
            )
            if ext_id:
                extensions_created += 1

    summary = {
        "ok": True,
        "tenant_id": tenant_id,
        "lookback_hours": hours,
        "rows_scanned": len(rows),
        "reviews_written": len(reviews),
        "failure_buckets": {k: len(v) for k, v in buckets.items()},
        "insights_upserted": insights_created,
        "extensions_promoted": extensions_created,
    }
    print("[attendance.learning.batch]", summary)
    return summary
