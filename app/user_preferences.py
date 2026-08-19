from __future__ import annotations

import json
import re
from typing import Any

from .db import get_conn

MAX_MEMORY_NOTES = 15
MAX_RECENT_TOPICS = 12

INTENT_TOPIC_LABELS: dict[str, str] = {
    "balance_inquiry": "saldo do cartão presente",
    "coupon_code": "código do cartão presente",
    "simulation": "simulação de uso do cartão",
    "raffle_history": "histórico de participações",
    "current_raffle": "sorteio aberto",
    "available_numbers": "números disponíveis",
    "rules_inquiry": "regras do sorteio",
    "preferred_name_update": "preferência de nome",
    "human_support": "atendimento humano",
    "commerce": "catálogo e produtos",
    "general_support": "dúvida geral",
}


def extract_first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    first = full_name.strip().split()[0]
    if len(first) < 2:
        return None
    return first[:1].upper() + first[1:].lower() if first.islower() or first.isupper() else first


def detect_preferred_name_update(text: str | None) -> str | None:
    if not text:
        return None

    patterns = (
        r"(?:me chame|me cham|pode me chamar|quero ser chamad[oa]|prefiro ser chamad[oa]) de\s+(.+?)[!.?\s]*$",
        r"(?:prefiro o nome|pode usar o nome)\s+(.+?)[!.?\s]*$",
    )
    normalized = text.strip()
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            preferred = match.group(1).strip(" \"'")
            if preferred:
                return preferred[:80]
    return None


def detect_speaking_style_update(text: str | None) -> str | None:
    if not text:
        return None

    normalized = text.strip().lower()
    rules: tuple[tuple[str, str], ...] = (
        (r"(?:fala|fale|seja|quero).{0,40}(?:mais )?formal", "formal"),
        (r"(?:fala|fale|seja|quero).{0,40}(?:mais )?(?:diret[oa]|curt[oa]|objetiv[oa])", "direto"),
        (r"(?:fala|fale|seja|quero).{0,40}(?:mais )?(?:descontra[ií]d[oa]|leve|engraçad[oa])", "descontraido"),
        (r"(?:sem formalidade|mais informal|pode ser informal|respostas mais formais)", "formal"),
    )
    for pattern, style in rules:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return style
    return None


def detect_memory_note(text: str | None) -> str | None:
    if not text:
        return None

    patterns = (
        r"(?:lembra(?:r)?(?: que| disso)?|anota(?:r| ai)?(?: que)?|guarda(?:r)?(?: que)?)\s+(.+?)[!.?\s]*$",
        r"(?:prefiro(?: que)?|gosto quando|quero que você)\s+(.+?)[!.?\s]*$",
        r"(?:n[ãa]o gosto quando|evita)\s+(.+?)[!.?\s]*$",
    )
    normalized = text.strip()
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            note = match.group(1).strip(" \"'")
            if len(note) >= 4:
                return note[:160]
    return None


def _default_preferences(user_id: int) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "preferred_name": None,
        "ask_preferred_name": False,
        "last_preferred_name_prompt_at": None,
        "speaking_style": None,
        "memory_notes": [],
        "recent_topics": [],
        "exists": False,
    }


def _normalize_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def get_user_preferences(user_id: int) -> dict[str, Any]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      user_id,
                      preferred_name,
                      ask_preferred_name,
                      last_preferred_name_prompt_at,
                      speaking_style,
                      memory_notes,
                      recent_topics
                    FROM public.ai_user_preferences
                    WHERE user_id = %(user_id)s
                    LIMIT 1
                    """,
                    {"user_id": user_id},
                )
                row = cur.fetchone()
                if not row:
                    return _default_preferences(user_id)
                return {
                    "user_id": row.get("user_id"),
                    "preferred_name": row.get("preferred_name"),
                    "ask_preferred_name": bool(row.get("ask_preferred_name")),
                    "last_preferred_name_prompt_at": row.get("last_preferred_name_prompt_at"),
                    "speaking_style": row.get("speaking_style"),
                    "memory_notes": _normalize_json_list(row.get("memory_notes")),
                    "recent_topics": _normalize_json_list(row.get("recent_topics")),
                    "exists": True,
                }
    except Exception:
        return _default_preferences(user_id)


def save_preferred_name(user_id: int, preferred_name: str) -> None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.ai_user_preferences
                      (user_id, preferred_name, ask_preferred_name, updated_at)
                    VALUES
                      (%(user_id)s, %(preferred_name)s, false, now())
                    ON CONFLICT (user_id) DO UPDATE SET
                      preferred_name = EXCLUDED.preferred_name,
                      ask_preferred_name = false,
                      updated_at = now()
                    """,
                    {"user_id": user_id, "preferred_name": preferred_name},
                )
    except Exception as exc:
        print("[user_preferences.save_preferred_name.error]", {
            "user_id": user_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })


def mark_preferred_name_prompted(user_id: int) -> None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.ai_user_preferences
                      (user_id, ask_preferred_name, last_preferred_name_prompt_at, updated_at)
                    VALUES
                      (%(user_id)s, false, now(), now())
                    ON CONFLICT (user_id) DO UPDATE SET
                      ask_preferred_name = false,
                      last_preferred_name_prompt_at = now(),
                      updated_at = now()
                    """,
                    {"user_id": user_id},
                )
    except Exception as exc:
        print("[user_preferences.mark_preferred_name_prompted.error]", {
            "user_id": user_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })


def save_speaking_style(user_id: int, speaking_style: str) -> None:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.ai_user_preferences
                      (user_id, speaking_style, updated_at)
                    VALUES
                      (%(user_id)s, %(speaking_style)s, now())
                    ON CONFLICT (user_id) DO UPDATE SET
                      speaking_style = EXCLUDED.speaking_style,
                      updated_at = now()
                    """,
                    {"user_id": user_id, "speaking_style": speaking_style[:40]},
                )
    except Exception as exc:
        print("[user_preferences.save_speaking_style.error]", {
            "user_id": user_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })


def append_memory_note(user_id: int, note: str) -> None:
    cleaned = (note or "").strip()
    if not cleaned:
        return

    preferences = get_user_preferences(user_id)
    notes = [item for item in preferences.get("memory_notes", []) if item.lower() != cleaned.lower()]
    notes.append(cleaned)
    notes = notes[-MAX_MEMORY_NOTES:]

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.ai_user_preferences
                      (user_id, memory_notes, updated_at)
                    VALUES
                      (%(user_id)s, %(memory_notes)s::jsonb, now())
                    ON CONFLICT (user_id) DO UPDATE SET
                      memory_notes = EXCLUDED.memory_notes,
                      updated_at = now()
                    """,
                    {"user_id": user_id, "memory_notes": json.dumps(notes, ensure_ascii=False)},
                )
    except Exception as exc:
        print("[user_preferences.append_memory_note.error]", {
            "user_id": user_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })


def append_recent_topic(user_id: int, topic: str) -> None:
    cleaned = (topic or "").strip()
    if not cleaned:
        return

    preferences = get_user_preferences(user_id)
    topics = [item for item in preferences.get("recent_topics", []) if item.lower() != cleaned.lower()]
    topics.append(cleaned)
    topics = topics[-MAX_RECENT_TOPICS:]

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.ai_user_preferences
                      (user_id, recent_topics, updated_at)
                    VALUES
                      (%(user_id)s, %(recent_topics)s::jsonb, now())
                    ON CONFLICT (user_id) DO UPDATE SET
                      recent_topics = EXCLUDED.recent_topics,
                      updated_at = now()
                    """,
                    {"user_id": user_id, "recent_topics": json.dumps(topics, ensure_ascii=False)},
                )
    except Exception as exc:
        print("[user_preferences.append_recent_topic.error]", {
            "user_id": user_id,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        })


def ensure_display_name_saved(user_id: int, account_name: str | None) -> None:
    preferences = get_user_preferences(user_id)
    if preferences.get("preferred_name"):
        return
    first_name = extract_first_name(account_name)
    if not first_name:
        return
    save_preferred_name(user_id, first_name)


def learn_from_incoming_message(user_id: int, text: str | None, account_name: str | None = None) -> None:
    ensure_display_name_saved(user_id, account_name)

    preferred_name = detect_preferred_name_update(text)
    if preferred_name:
        save_preferred_name(user_id, preferred_name)

    speaking_style = detect_speaking_style_update(text)
    if speaking_style:
        save_speaking_style(user_id, speaking_style)

    memory_note = detect_memory_note(text)
    if memory_note:
        append_memory_note(user_id, memory_note)


def record_interaction_memory(user_id: int, intent: str | None, user_text: str | None) -> None:
    topic = INTENT_TOPIC_LABELS.get(intent or "")
    if not topic:
        cleaned = (user_text or "").strip()
        if cleaned:
            topic = cleaned[:80]
    if topic:
        append_recent_topic(user_id, topic)


def resolve_display_name(account_name: str | None, preferences: dict[str, Any]) -> str | None:
    preferred = (preferences.get("preferred_name") or "").strip()
    if preferred:
        return preferred
    return extract_first_name(account_name)


def build_memory_context(preferences: dict[str, Any]) -> str:
    lines: list[str] = []

    preferred_name = (preferences.get("preferred_name") or "").strip()
    if preferred_name:
        lines.append(f"- Nome preferido para tratamento: {preferred_name}")

    speaking_style = (preferences.get("speaking_style") or "").strip()
    if speaking_style:
        lines.append(f"- Modo de falar preferido: {speaking_style}")

    notes = preferences.get("memory_notes") or []
    if notes:
        lines.append("- Preferências já anotadas:")
        for note in notes[-8:]:
            lines.append(f"  • {note}")

    topics = preferences.get("recent_topics") or []
    if topics:
        lines.append(f"- Assuntos recentes do cliente: {', '.join(topics[-6:])}")

    if not lines:
        return ""

    return (
        "Memória do cliente (use para personalizar o atendimento):\n"
        + "\n".join(lines)
        + "\n- Não pergunte de novo como prefere ser chamado se o nome já estiver acima."
        + "\n- Adapte tom e tamanho da resposta ao estilo preferido, sem repetir informações desnecessárias."
    )


def enrich_customer_context(customer_context: dict[str, Any]) -> dict[str, Any]:
    if not customer_context.get("found") or not customer_context.get("user_id"):
        return customer_context

    user_id = int(customer_context["user_id"])
    preferences = get_user_preferences(user_id)
    enriched = dict(customer_context)
    enriched["preferences"] = preferences
    enriched["memory_context"] = build_memory_context(preferences)
    enriched["display_name"] = resolve_display_name(customer_context.get("name"), preferences)
    return enriched
