"""Allowlisted order facts from Mercos V2; never persist customer/contact data."""
from decimal import Decimal, InvalidOperation


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return float(parsed) if parsed.is_finite() and parsed >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def normalize_contents(row):
    raw = row.get("itens")
    if not isinstance(raw, list):
        return None
    items = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        if item.get("excluido") is True:
            continue
        product_id = item.get("produto_id")
        quantity = number(item.get("quantidade"))
        if product_id is None or quantity is None:
            return None
        items.append({
            "product_id": str(product_id),
            "name": str(item.get("produto_nome") or "").strip() or None,
            "reference": str(item.get("produto_codigo") or "").strip() or None,
            "quantity": quantity,
            "unit_price": number(item.get("preco_liquido")),
            "subtotal": number(item.get("subtotal")),
        })
    return {"items": items, "items_confirmed": True, "item_count": len(items),
            "total": number(row.get("total")), "shipping_price": number(row.get("valor_frete"))}
