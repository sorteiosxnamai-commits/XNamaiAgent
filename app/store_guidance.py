"""Grounded guidance for broad, multi-topic questions about the store."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .site_knowledge import STORE_URL

if TYPE_CHECKING:
    from .models import SalesInterpretation


def _fold(text: str | None) -> str:
    value = unicodedata.normalize("NFKD", (text or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


_OVERVIEW_PATTERNS = (
    re.compile(r"\b(?:sobre|conhecer|ver) (?:os |seus |as )?produtos\b"),
    re.compile(r"\bo que (?:voces|a xnamai) (?:vende|vendem|tem|trabalha)\b"),
    re.compile(r"\b(?:quais|que) produtos\b"),
    re.compile(r"\blinha(?:s)? de produtos\b"),
    re.compile(r"\bcomo (?:funciona|trabalham) (?:o )?(?:atacado|varejo|preco|catalogo)\b"),
)

_COMMERCIAL_TOPICS: dict[str, tuple[str, ...]] = {
    "price": ("preco", "precos", "valor", "valores"),
    "wholesale": ("atacado", "lojista", "lojistas", "revenda", "revendedor", "revendedores"),
    "retail": ("varejo", "consumidor final"),
    "catalog": ("catalogo", "produtos", "linha de produtos"),
    "registration": ("cadastro", "cadastrar", "cpf", "cnpj"),
}

_PRODUCT_CATEGORIES = (
    "fone", "fones", "mouse", "mouses", "carregador", "carregadores",
    "cabo", "cabos", "acessorio", "acessorios", "eletronico", "eletronicos",
    "papelaria", "pet", "cosmetico", "cosmeticos", "bicicleta", "bicicletas",
    "utilidade", "utilidades",
)

_CATEGORY_LABELS = {
    "fone": "fones de ouvido",
    "fones": "fones de ouvido",
    "mouse": "mouses",
    "mouses": "mouses",
    "carregador": "carregadores",
    "carregadores": "carregadores",
    "cabo": "cabos",
    "cabos": "cabos",
    "acessorio": "acessórios",
    "acessorios": "acessórios",
    "eletronico": "eletrônicos",
    "eletronicos": "eletrônicos",
    "papelaria": "papelaria",
    "pet": "produtos pet",
    "cosmetico": "cosméticos",
    "cosmeticos": "cosméticos",
    "bicicleta": "bicicletas elétricas",
    "bicicletas": "bicicletas elétricas",
    "utilidade": "utilidades",
    "utilidades": "utilidades",
}


@dataclass(frozen=True)
class StoreGuidance:
    reply_text: str
    topics: tuple[str, ...]


def _mentioned_topics(folded: str) -> set[str]:
    return {
        topic
        for topic, terms in _COMMERCIAL_TOPICS.items()
        if any(re.search(rf"\b{re.escape(term)}\b", folded) for term in terms)
    }


def _mentioned_categories(folded: str) -> set[str]:
    return {
        category
        for category in _PRODUCT_CATEGORIES
        if re.search(rf"\b{re.escape(category)}\b", folded)
    }


def is_broad_store_question(
    text: str | None,
    interpretation: "SalesInterpretation | None" = None,
) -> bool:
    """Distinguish commercial orientation from a concrete catalog lookup."""
    folded = _fold(text)
    if not folded:
        return False

    if interpretation is not None:
        subject = interpretation.subject
        if subject.reference or subject.ean or subject.model:
            return False
        if interpretation.goal in {"inspect", "compare", "buy", "after_sales"}:
            return False
        preferences = interpretation.preferences
        if any((
            preferences.budget_min is not None,
            preferences.budget_max is not None,
            preferences.color,
            preferences.style,
            preferences.material,
            preferences.attributes,
        )):
            return False

    overview_phrase = any(pattern.search(folded) for pattern in _OVERVIEW_PATTERNS)
    topics = _mentioned_topics(folded)
    categories = _mentioned_categories(folded)
    multi_topic_question = (
        len(categories) >= 2
        and bool(topics.intersection({"price", "wholesale", "retail"}))
    )
    store_model_question = (
        "catalog" in topics
        and len(topics.intersection({"price", "wholesale", "retail", "registration"})) >= 2
    )
    return overview_phrase or multi_topic_question or store_model_question


def _salutation(text: str | None) -> str | None:
    folded = _fold(text)
    if "boa tarde" in folded:
        return "Boa tarde! 😊"
    if "bom dia" in folded:
        return "Bom dia! 😊"
    if "boa noite" in folded:
        return "Boa noite! 😊"
    if re.search(r"\b(?:oi|ola)\b", folded):
        return "Olá! 😊"
    return None


def _natural_join(values: list[str]) -> str:
    if len(values) < 2:
        return values[0] if values else ""
    return ", ".join(values[:-1]) + f" e {values[-1]}"


def build_store_guidance(
    text: str | None,
    interpretation: "SalesInterpretation | None" = None,
) -> StoreGuidance | None:
    """Answer broad store questions with useful official context and one CTA."""
    if not is_broad_store_question(text, interpretation):
        return None

    folded = _fold(text)
    topics = _mentioned_topics(folded)
    category_tokens = _mentioned_categories(folded)
    category_labels = list(dict.fromkeys(
        _CATEGORY_LABELS[token]
        for token in _PRODUCT_CATEGORIES
        if token in category_tokens
    ))
    blocks: list[str] = []
    greeting = _salutation(text)
    if greeting:
        blocks.append(greeting)

    positioning = (
        "A XNamai trabalha principalmente com atacado, atendendo lojistas e "
        "revendedores. Também aceita cadastro e compras com CPF ou CNPJ."
        if topics.intersection({"wholesale", "retail", "registration"})
        else "A XNamai é uma distribuidora de eletrônicos e acessórios voltada principalmente ao atacado."
    )
    blocks.append(positioning)

    assortment = (
        f"Trabalhamos com {_natural_join(category_labels)} e outras linhas de "
        "eletrônicos, acessórios, utilidades e produtos de giro."
        if category_labels
        else (
            "Trabalhamos com eletrônicos e acessórios, carregadores, cabos, "
            "utilidades, papelaria, produtos pet, cosméticos e outras linhas "
            "de giro."
        )
    )
    blocks.append(
        assortment
        + " Os preços e a disponibilidade atualizados ficam no catálogo oficial: "
        + STORE_URL
    )

    if topics.intersection({"price", "wholesale", "retail", "registration"}):
        blocks.append("O pedido mínimo normal é de R$ 800,00.")

    if topics.intersection({"wholesale", "retail", "registration"}):
        blocks.append(
            "Você pretende comprar com CPF ou CNPJ? Assim te oriento pelo caminho certo."
        )
    else:
        blocks.append(
            "Qual categoria ou tipo de produto você procura? Assim consigo direcionar melhor."
        )
    return StoreGuidance(reply_text="\n\n".join(blocks), topics=tuple(sorted(topics)))
