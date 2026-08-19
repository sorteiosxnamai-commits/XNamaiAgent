from __future__ import annotations

import re
from typing import Any

from .models import AgentResult, IncomingMessage
from .tray_tools import execute_tool


COMMERCE_UNAVAILABLE = "N\u00e3o consegui consultar as informa\u00e7\u00f5es da loja neste momento. Tente novamente em instantes."


_CURRENT_PRODUCT_BY_CONTEXT: dict[str, dict[str, Any]] = {}


def clear_commerce_memory() -> None:
    _CURRENT_PRODUCT_BY_CONTEXT.clear()


def _context_key(message: IncomingMessage) -> str | None:
    return message.conversation_id or message.sender_key or message.sender_phone


def _remember_product(message: IncomingMessage, product: dict[str, Any]) -> None:
    key = _context_key(message)
    product_id = product.get("id")
    if not key or not product_id:
        return
    _CURRENT_PRODUCT_BY_CONTEXT[key] = {
        field: product.get(field)
        for field in ("id", "name", "reference", "ean", "brand")
        if product.get(field) is not None
    }


def _remembered_product(message: IncomingMessage) -> dict[str, Any] | None:
    key = _context_key(message)
    return _CURRENT_PRODUCT_BY_CONTEXT.get(key) if key else None


def extract_product_query(text: str | None) -> str:
    value = " ".join((text or "").strip().split())
    value = re.sub(r"^e\s+", "", value, count=1, flags=re.IGNORECASE)
    prefixes = (
        r"gostaria\s+de\s+(?:comprar\s+)?", r"quero\s+(?:comprar\s+|adquirir\s+)?", r"procuro\s+", r"busco\s+",
        r"voc\u00eas\s+t\u00eam", r"voces\s+tem", r"voc\u00eas\s+tem",
        r"voc\u00eas\s+vendem", r"voces\s+vendem", r"tem\s+estoque\s+(?:de|do|da)",
        r"tem\s+estoque", r"qual\s+o\s+pre\u00e7o\s+(?:de|do|da)",
        r"qual\s+o\s+preco\s+(?:de|do|da)", r"qual\s+o\s+pre\u00e7o", r"qual\s+o\s+preco",
        r"quanto\s+custa", r"quanto\s+fica", r"vende", r"vendem", r"tem\s+(?:o|a|um|uma)", r"tem",
    )
    for prefix in prefixes:
        before_prefix = value
        value = re.sub(rf"^\s*{prefix}\s*", "", value, count=1, flags=re.IGNORECASE)
        if value != before_prefix:
            break
    value = value.strip(" ?!.,;:")
    value = re.sub(r"^(?:o|a|um|uma)\s+", "", value, flags=re.IGNORECASE)
    return value.strip(" ?!.,;:")


def _is_follow_up_without_product(query: str) -> bool:
    normalized = query.lower().strip()
    return not normalized or normalized in {
        "desse relogio", "desse relógio", "desse produto", "deste produto",
        "dele", "dela", "esse produto", "esse relogio", "esse relógio",
    } or normalized in {"estoque", "disponibilidade", "disponivel", "pix", "no pix", "e no pix", "quanto fica", "quanto fica no pix", "parcelamento", "parcelar", "promocao"}


def is_deictic_product_price_request(text: str | None) -> bool:
    """True for 'qual o preço desse/da foto' — must not reuse a stale SKU."""
    normalized = (text or "").casefold()
    if not normalized.strip():
        return False
    photo_markers = (
        "da foto",
        "na foto",
        "dessa foto",
        "nessa foto",
        "do relogio da foto",
        "do relógio da foto",
        "da imagem",
    )
    this_markers = (
        "desse",
        "dessa",
        "deste",
        "desta",
        "esse relogio",
        "esse relógio",
        "esse produto",
        "este relogio",
        "este relógio",
        "este produto",
    )
    return any(marker in normalized for marker in photo_markers + this_markers)


def resolve_commerce_action(text: str | None) -> str | None:
    normalized = (text or "").lower()
    if any(term in normalized for term in ("cupom comercial", "cupom disponível", "cupom disponivel", "algum cupom")):
        return "coupon_search"
    if any(term in normalized for term in ("estoque", "disponibilidade", "disponível", "disponivel")):
        return "product_inventory"
    if any(term in normalized for term in ("pix", "parcelamento", "parcelar", "promocao", "promoção")):
        return "product_price"
    if any(
        term in normalized
        for term in (
            "quanto custa",
            "qual o preço",
            "qual o preco",
            "preço",
            "preco",
            "valor",
        )
    ):
        return "product_price"
    if any(term in normalized for term in ("tem ", "vocês têm", "voces tem", "vende", "produto", "relógio", "relogio", "marca", "modelo", "sku", "ean")):
        return "product_search"
    return None


def _log_route(action: str, tool: str, has_query: bool) -> None:
    print("[commerce.route]", {"action": action, "tool": tool, "has_query": has_query})


def _products(result: dict[str, Any]) -> list[dict[str, Any]]:
    products = result.get("products") if isinstance(result, dict) else None
    return [item for item in products[:3] if isinstance(item, dict)] if isinstance(products, list) else []


def _price(product: dict[str, Any]) -> Any:
    for key in ("current_price", "promotional_price", "price"):
        if product.get(key) is not None:
            return product[key]
    return None


def _price_label(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return str(value)


def _payment_label(value: Any, *, compact: bool = False) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value[:120] if compact else value
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    pix = value.get("pix")
    if isinstance(pix, dict) and pix.get("value") is not None:
        parts.append(f"Pix: {_price_label(pix['value'])}")
    installments = value.get("installments")
    if isinstance(installments, list):
        limit = 1 if compact else 3
        for item in installments[:limit]:
            if not isinstance(item, dict):
                continue
            count = item.get("count") or "?"
            amount = _price_label(item.get("value"))
            interest = " com juros" if item.get("interest") else " sem juros"
            parts.append(f"{count}x{interest}" + (f" de {amount}" if amount else ""))
    return ", ".join(parts) or None


def _product_lines(
    products: list[dict[str, Any]],
    inventory: dict[str, Any] | None = None,
    *,
    compact: bool = False,
) -> list[str]:
    lines: list[str] = []
    for product in products:
        name = product.get("name") or "Produto encontrado"
        parts = [str(name)]
        if product.get("reference"):
            parts.append(f"Ref.: {product['reference']}")
        price = _price(product)
        if price is not None:
            parts.append(f"Pre\u00e7o: {_price_label(price)}")
        product_url = (
            product.get("url")
            or product.get("product_url")
            or product.get("link")
        )
        if isinstance(product_url, str) and product_url.strip():
            parts.append(f"Link: {product_url.strip()}")
        if not compact:
            payment = _payment_label(
                product.get("payment_option_details")
            ) or _payment_label(product.get("payment_option"))
            if payment:
                parts.append(f"Condi\u00e7\u00f5es comerciais: {payment}")
        if inventory:
            if inventory.get("stock") is not None:
                parts.append(f"Estoque: {inventory['stock']}")
            if inventory.get("availability"):
                parts.append(f"Disponibilidade: {inventory['availability']}")
            for key, label in (("available_for_purchase", "Dispon\u00edvel para compra"), ("upon_request", "Sob consulta")):
                if inventory.get(key) is not None:
                    parts.append(f"{label}: {inventory[key]}")
        elif product.get("stock") is not None:
            parts.append(f"Estoque: {product['stock']}")
        lines.append(" | ".join(parts))
    return lines


def _product_result(action: str, products: list[dict[str, Any]]) -> AgentResult:
    if not products:
        return AgentResult(reply_text="N\u00e3o encontrei esse produto no cat\u00e1logo agora.", intent="commerce", handoff_required=False, safety_reason="product_not_found")
    if action == "product_disambiguation":
        prefix = "Encontrei algumas possibilidades:"
    else:
        prefix = "Sim, encontrei:" if action != "product_price" else "Encontrei:"
    numbered_lines = [
        f"{position}. {line}"
        for position, line in enumerate(_product_lines(products, compact=True), start=1)
    ]
    suffix = "\n\nÉ algum desses?" if action == "product_disambiguation" else ""
    return AgentResult(
        reply_text=prefix + "\n" + "\n".join(numbered_lines) + suffix,
        intent="commerce",
        handoff_required=False,
        commercial_data={"products": products},
    )


async def handle_commerce_message(
    message: IncomingMessage,
    facts: dict[str, Any],
    customer_context: dict[str, Any],
    *,
    action: str | None = None,
    query: str | None = None,
) -> AgentResult | None:
    del customer_context
    action = action or resolve_commerce_action(message.text)
    if not action:
        return None

    query = query if query is not None else extract_product_query(message.text)
    if action == "coupon_search":
        _log_route(action, "list_coupons", bool(query))
        result = await execute_tool("list_coupons", {"limit": 3})
        if "error" in result:
            return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
        coupons = result.get("coupons") if isinstance(result.get("coupons"), list) else []
        if not coupons:
            return AgentResult(reply_text="N\u00e3o encontrei cupons comerciais dispon\u00edveis agora.", intent="commerce", handoff_required=False, safety_reason="coupon_not_found")
        lines = [f"{coupon.get('code') or 'Cupom'}: {coupon.get('description') or 'dispon\u00edvel para consulta'}" for coupon in coupons[:3] if isinstance(coupon, dict)]
        return AgentResult(reply_text="Encontrei estes cupons comerciais:\n" + "\n".join(lines), intent="commerce", handoff_required=False)

    remembered = _remembered_product(message)
    if _is_follow_up_without_product(query):
        if not remembered or not remembered.get("id"):
            return AgentResult(
                reply_text="Qual produto você quer consultar? Informe o nome, modelo ou referência.",
                intent="commerce",
                handoff_required=False,
                safety_reason="product_context_missing",
            )
        product_id = str(remembered["id"])
        if action == "product_inventory":
            _log_route(action, "check_inventory", False)
            inventory = await execute_tool("check_inventory", {"product_id": product_id})
            if "error" in inventory:
                return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
            return AgentResult(reply_text="Consulta de estoque:\n" + "\n".join(_product_lines([remembered], inventory)), intent="commerce", handoff_required=False, commercial_data={"products": [remembered], "inventory": inventory})
        _log_route(action, "get_product", False)
        current = await execute_tool("get_product", {"product_id": product_id})
        if "error" in current:
            return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
        identity = {key: remembered.get(key) for key in ("id", "name", "reference", "ean", "brand") if remembered.get(key) is not None}
        return _product_result(action, [{**identity, **current}])

    _log_route(action, "search_products", True)
    search = await execute_tool("search_products", {"query": query, "limit": 3})
    if "error" in search:
        return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
    products = _products(search)
    if action == "product_price" and len(products) == 1 and products[0].get("id"):
        _log_route(action, "get_product", True)
        current = await execute_tool("get_product", {"product_id": str(products[0]["id"])})
        if "error" in current:
            return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
        identity = {key: products[0].get(key) for key in ("id", "name", "reference", "ean", "brand") if products[0].get(key) is not None}
        detail = {**identity, **current}
        _remember_product(message, detail)
        return _product_result(action, [detail])
    if action != "product_inventory":
        if len(products) == 1:
            _remember_product(message, products[0])
        return _product_result(action, products)
    if not products:
        return _product_result(action, products)
    if len(products) != 1:
        return AgentResult(reply_text="Encontrei mais de um produto com esse termo. Pode informar a refer\u00eancia ou o modelo exato?", intent="commerce", handoff_required=False, safety_reason="ambiguous_product")

    product_id = products[0].get("id")
    if not product_id:
        return AgentResult(reply_text="N\u00e3o consegui identificar esse produto para confirmar o estoque.", intent="commerce", handoff_required=False, safety_reason="product_id_missing")
    _remember_product(message, products[0])
    _log_route(action, "check_inventory", True)
    inventory = await execute_tool("check_inventory", {"product_id": str(product_id)})
    if "error" in inventory:
        return AgentResult(reply_text=COMMERCE_UNAVAILABLE, intent="commerce", handoff_required=False, safety_reason="tray_adapter_unavailable")
    return AgentResult(reply_text="Consulta de estoque:\n" + "\n".join(_product_lines(products, inventory)), intent="commerce", handoff_required=False, commercial_data={"products": products, "inventory": inventory})
