"""Existing-order follow-ups take precedence over catalog selection."""
from __future__ import annotations

import re

from .models import AgentResult
from .order_service import _fold_text, extract_order_reference, get_order_facts


def resolve_order_query(text, state):
    value = _fold_text(text)
    if re.search(r"\b(carrinho|rascunho|adiciona|coloca|comprar|fazer um pedido)\b", value):
        return None
    contents = bool(re.search(r"\b(itens?|produtos?|completo|detalhes|resumo|quantidade)\b", value))
    # The existing status/payment route retains its reconciliation logic.
    explicit = bool(re.search(
        r"\b(?:itens?|produtos?|quantidade)\b.*\b(?:do|desse|deste|no|meu|seu)\s+pedido\b"
        r"|\bpedido\s*(?:\d+\s*)?(?:completo|detalhado)\b"
        r"|\b(?:listar|detalhar|resumo|detalhes|conteudo)\b.*\bpedido\b", value
    ))
    topic = _fold_text(state.active_topic)
    in_order = bool(state.order_id or state.order_lookup_id) and (
        "order" in topic or "pedido" in topic
    )
    if "pedido" not in value:
        from .commerce.catalog_filters import CATEGORIES
        if any(re.search(pattern, value) for pattern in CATEGORIES.values()):
            return None
    continuation = in_order and bool(re.search(
        r"^(?:mais|continue|continua|proximos|e os outros|so tem|tem mais|quantos|quais|(?:pode )?consulta|verifica|confere|nao precisa.*consulta)", value
    ) or re.fullmatch(r"#?\d{3,10}", value.strip()))
    if in_order and topic == "order_contents" and state.order_items_offset and value.strip(" .!?") == "sim":
        continuation = True
    if not (explicit or continuation):
        return None
    reference = extract_order_reference(text)
    if not reference and continuation:
        numbers = re.findall(r"(?<!\d)\d{3,10}(?!\d)", value)
        if len(numbers) == 1:
            reference = numbers[0]
    next_page = bool(in_order and re.fullmatch(r"(?:sim|mais|continue|continua|proximos)(?:\s+(?:itens|do pedido))?[.!? ]*", value))
    return {"reference": reference, "contents": contents or continuation and not reference,
            "next_page": next_page}


def money(value):
    return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


async def handle_order_query(text, *, state, execute):
    query = resolve_order_query(text, state)
    if query is None:
        return None
    reference = query["reference"] or state.order_id or state.order_lookup_id
    if not reference:
        return AgentResult(reply_text="Me informe o número do pedido para eu consultar. 😊", intent="commerce",
            response_metadata={"domain": "commerce", "active_topic": "order_status"})
    result = await get_order_facts(state=state, execute=execute, order_id=reference)
    result.response_metadata["active_topic"] = "order_contents" if query["contents"] else "order_status"
    result.response_metadata["response_source"] = "verified_order_query"
    result.response_metadata["clear_active_product"] = True
    if not query["contents"] or not result.commercial_data.get("success"):
        return result
    facts = result.commercial_data
    if facts.get("items_confirmed") is not True:
        result.reply_text = (
            "Consegui consultar o status, mas os itens completos desse pedido ainda não estão disponíveis para consulta. "
            "Não consigo confirmar a quantidade de itens agora."
        )
        result.response_metadata["factual_fallback_text"] = result.reply_text
        return result
    items = facts["items"]
    label = facts.get("order_number") or facts["order_id"]
    start = state.order_items_offset if query["next_page"] else 0
    if query["next_page"] and start >= len(items):
        result.reply_text = "Já mostrei todos os itens confirmados desse pedido. 📋"
        result.response_metadata["factual_fallback_text"] = result.reply_text
        return result
    lines = [f"📋 Pedido {label} — {facts.get('status') or 'status não informado'}", f"{len(items)} itens no pedido:"]
    stop = start
    for index, item in enumerate(items[start:], start=start):
        line = f"{index + 1}. {item.get('name') or 'Produto ' + item['product_id']}"
        if item.get("reference"):
            line += f" | Ref.: {item['reference']}"
        line += f" | Quantidade: {item['quantity']:g}"
        if item.get("unit_price") is not None:
            line += f" | Unitário no pedido: {money(item['unit_price'])}"
        if item.get("subtotal") is not None:
            line += f" | Subtotal: {money(item['subtotal'])}"
        if stop > start and len("\n".join(lines)) + len(line) > 2800:
            break
        lines.append(line)
        stop = index + 1
    if not items:
        lines.append("A Mercos retornou este pedido sem itens ativos.")
    if facts.get("total") is not None:
        lines.append(f"Total registrado do pedido: {money(facts['total'])}")
    if stop < len(items):
        lines.append(f"Mostrei os itens {start + 1} a {stop}. Diga 'continue' para ver os próximos. 😊")
    result.reply_text = "\n".join(lines)
    result.response_metadata.update({"preserve_complete_list": True, "factual_fallback_text": result.reply_text,
        "order_contents_requested": True})
    result.response_metadata["order_state"]["order_items_offset"] = stop
    return result
