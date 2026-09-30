"""Complete category browsing with persistent continuation and strict refinements."""
from __future__ import annotations

import re

from .commerce.catalog_filters import CATEGORIES, fold, matches
from .commerce.product_facts import without_unconfirmed_facts
from .models import AgentResult
from .order_queries import money

MORE = r"(?:mais|continue|continua|proximos|mostrar mais|ver mais|tem mais|mais produtos|mais opcoes)[?.! ]*"


def requested_queries(text, state, interpretation=None):
    value = fold(text).strip()
    if re.search(r"\b(pedido|carrinho|adiciona|adicione|adicionar|coloca|coloque|retira|remove|finalizar|foto|imagem|comprar|comprei|troca|trocar|devolver|devolucao|garantia|defeito|quebrou|reembolso)\b", value):
        return None
    if re.search(r"\b(esse|essa|este|esta|ele|ela)\b", value) and state.active_product:
        return None
    if re.search(r"\b(?:quero|preciso|separa|separe)\s+\d+\s+(?:unidades?\s+(?:de\s+)?)?", value):
        return None
    if interpretation is not None and (interpretation.purchase_action or interpretation.order_action or interpretation.payment_action):
        return None
    previous = (state.catalog_listing or {}).get("queries") or []
    if previous and re.fullmatch(MORE, value):
        return [dict(q) for q in previous]
    found = []
    for category, pattern in CATEGORIES.items():
        for match in re.finditer(pattern, value):
            found.append((match.start(), match.end(), category))
    found.sort()
    # A category after "com/para" is a requested feature, not another group
    # ("caixa de som com microfone", "cabo para mouse").
    groups = []
    for candidate in found:
        if groups:
            between = value[groups[-1][1]:candidate[0]]
            if not re.search(r"[,;/]|\b(?:e|ou)\s*$", between):
                continue
        groups.append(candidate)
    found = groups
    queries = []
    for index, (start, end, category) in enumerate(found):
        rest = value[end:found[index + 1][0] if index + 1 < len(found) else len(value)]
        rest = re.split(r"\b(?:e preco|preco de compra|no atacado|no varejo)\b", rest)[0]
        rest = re.sub(r"\s+por favor\b.*$", "", rest)
        # Availability is a question about the catalog, not a product feature.
        rest = re.sub(r"\b(?:(?:estao|esta|tem|ha|voces tem|voces possuem)\s+)?disponive(?:is|l)\b.*$", "", rest)
        rest = re.sub(r"(?:\s+e\s*)?$", "", rest.strip(" ,.;?!"))
        query = (category + " " + rest).strip()
        if query not in [q["query"] for q in queries]:
            queries.append({"query": query, "label": query, "offset": 0, "done": False})
    if queries:
        return queries
    if previous and len(previous) == 1 and re.match(r"^(?:so\b|somente\b|apenas\b|de\b|com\b|sem\b|\d+\s*(?:w|watts?)\b)", value):
        refine = re.sub(r"^(?:(?:so|somente|apenas|quero|de)\s+)+", "", value).strip(" .!?")
        base = previous[0]["query"]
        if re.search(r"\d+(?:[.,]\d+)?\s*(?:w|watts?)\b", refine):
            base = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:w|watts?)\b", "", base)
        query = " ".join((base + " " + refine).split())
        return [{"query": query, "label": query, "offset": 0, "done": False}]
    if re.search(r"\b(?:quais produtos|que produtos|listar produtos|lista de produtos|mostrar produtos|ver catalogo|mostra o catalogo)\b", value):
        return [{"query": "", "label": "Catálogo", "offset": 0, "done": False}]
    # The interpreter can identify categories beyond the common alias vocabulary.
    subject = getattr(interpretation, "subject", None)
    category = getattr(subject, "product_type", None)
    if category and getattr(interpretation, "goal", None) in {"discover", "find"}:
        query = " ".join(str(v) for v in (category, getattr(subject, "brand", None), getattr(subject, "model", None)) if v)
        return [{"query": query, "label": query, "offset": 0, "done": False}]
    return None


async def handle_wholesale_catalog(text, *, state, execute, interpretation=None, queries_override=None):
    queries = queries_override if queries_override is not None else requested_queries(text, state, interpretation)
    if queries is None:
        return None
    if all(q.get("done") for q in queries):
        return AgentResult(reply_text="Já mostrei todos os produtos encontrados nesses filtros. 😊 Você pode escolher outra categoria ou ajustar as características.",
            intent="commerce", response_metadata={"domain": "commerce", "active_topic": "product_catalog"})
    sections, presented = [], []
    failed = False
    # A page is bounded by the channel's message size, not by a catalog-wide cap.
    active = sum(not q.get("done") for q in queries)
    budget = max(350, 2700 // max(1, active))
    for q in queries:
        if q.get("done"):
            continue
        if len("\n\n".join(sections)) > 2500:
            break
        try:
            response = await execute("search_products", {"query": q["query"], "strict": True,
                "offset": q.get("offset", 0), "limit": 20})
        except Exception:
            response = {"ok": False}
        if response.get("ok") is not True:
            sections.append(f"*{q['label']}*: não consegui consultar agora; posso tentar novamente.")
            failed = True
            continue
        rows = response.get("products") or []
        lines = [f"*{q['label'] or 'Catálogo'}*"]
        consumed = 0
        for raw in rows:
            # Mandatory filters cannot silently disappear in an adapter fallback.
            if not matches(q["query"], raw):
                failed = True
                break
            product = without_unconfirmed_facts(raw)
            line = f"{len(presented) + 1}. {product.get('name') or product.get('id')}"
            if product.get("reference"):
                line += f" | Ref.: {product['reference']}"
            if product.get("price") is not None:
                line += " | " + money(product["price"])
            if product.get("stock") is not None:
                line += f" | Estoque: {product['stock']}"
            if consumed and len("\n".join(lines)) + len(line) > budget:
                break
            lines.append(line)
            presented.append(product)
            consumed += 1
        q["offset"] = q.get("offset", 0) + consumed
        q["done"] = consumed == len(rows) and not response.get("paging", {}).get("has_more", len(rows) == 20)
        if not rows:
            lines.append("Não encontrei correspondências para esses filtros no catálogo consultado.")
        if consumed < len(rows) and consumed == 0:
            lines.append("Não consegui confirmar produtos que atendam a todos esses filtros.")
        sections.append("\n".join(lines))
    more = any(not q.get("done") for q in queries)
    footer = "\n\nHá mais resultados. Diga 'continue' para receber os próximos, mantendo os filtros."
    text_reply = "Separei por categoria para você 😊\n\n" + "\n\n".join(sections)
    if more and not failed:
        text_reply += footer
    return AgentResult(reply_text=text_reply, intent="commerce", commercial_data={"products": presented},
        response_metadata={"domain": "commerce", "goal": "find", "active_topic": "product_catalog",
            "response_source": "wholesale_catalog", "used_commerce_provider": True,
            "presented_products": True, "clear_active_product": True, "preserve_complete_list": True,
            "catalog_listing": {"queries": queries}, "factual_fallback_text": text_reply})
