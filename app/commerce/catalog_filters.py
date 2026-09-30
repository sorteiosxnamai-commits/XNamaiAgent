"""Category aliases and mandatory lexical constraints for wholesale searches."""
import re
import unicodedata


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


CATEGORIES = {
    "fone de ouvido": r"\b(?:fones?(?: de ouvido)?|headsets?|headphones?|earbuds?)\b",
    "mouse": r"\bmouses?\b(?!\s*pad\b)",
    "caixa de som": r"\b(?:caixas? (?:de som|amplificadas?)|alto[- ]falantes?|speakers?)\b",
    "teclado": r"\bteclados?\b",
    "carregador": r"\bcarregador(?:es)?\b",
    "cabo": r"\bcabos?\b",
    "pelicula": r"\bpeliculas?\b",
    "capa": r"\b(?:capas?|capinhas?)\b",
    "suporte": r"\bsuportes?\b",
    "microfone": r"\bmicrofones?\b",
    "smartwatch": r"\b(?:smartwatch(?:es)?|relogios? inteligentes?)\b",
    "power bank": r"\b(?:power\s*bank|baterias? portateis?)\b",
}


def constraints(query):
    value = fold(query)
    patterns = []
    categories = [(match.start(), category) for category, pattern in CATEGORIES.items()
                  if (match := re.search(pattern, value))]
    primary = min(categories)[1] if categories else None
    if primary in {"fone de ouvido", "mouse", "caixa de som"}:
        # Mentions on accessories/decorations do not make them the device itself.
        # Keep bundles supplied WITH a case/cable: the accessory must precede
        # the device name. "cabo para fone" remains a valid cable query.
        accessory = r"\b(?:suportes?|luminarias?|bonecos?|estojos?|cases?|capas?|capinhas?|adaptador(?:es)?|cabos?|almofadas?)\b"
        patterns.append(r"^(?!.*" + accessory + r".*" + CATEGORIES[primary] + r")")
    for category, pattern in CATEGORIES.items():
        if re.search(pattern, value):
            patterns.append(pattern)
            value = re.sub(pattern, " ", value)
    if re.search(r"\bsem fio\b", value):
        patterns.append(r"\b(?:sem fio|wireless|bluetooth|bt)\b")
        value = re.sub(r"\bsem fio\b", " ", value)
    if re.search(r"\bcom fio\b", value):
        patterns.append(r"\bcom fio\b")
        value = re.sub(r"\bcom fio\b", " ", value)
    for power in re.findall(r"\b(\d+(?:[.,]\d+)?)\s*(?:w|watts?)\b", value):
        magnitude = "[.,]".join(re.escape(part) for part in re.split(r"[.,]", power))
        patterns.append(r"(?<![\d.,])" + magnitude + r"\s*(?:w(?:atts?)?)(?:\b|rms\b)")
    value = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:w|watts?)\b", " ", value)
    stop = {"de", "da", "do", "das", "dos", "a", "o", "as", "os", "e", "um", "uma", "para", "por", "com", "marca", "potencia"}
    for token in re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", value):
        if token not in stop:
            patterns.append(r"\b" + re.escape(token) + r"\b")
    return patterns


def matches(query, product):
    text = fold(" ".join(str(product.get(k) or "") for k in ("name", "reference", "model", "brand")))
    return all(re.search(pattern, text) for pattern in constraints(query))


def postgres_pattern(pattern):
    return pattern.replace(r"\b", r"\y")
