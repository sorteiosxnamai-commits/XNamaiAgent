from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel

from .config import get_settings
from .models import SalesInterpretation
from .openai_runtime import execute_openai_call
from .turn_runtime import LLMCallBudgetExceeded


PRODUCT_PAGE_LIMIT = 20
CATALOG_DISCOVERY_MAX_PAGES = 5
CATALOG_DISCOVERY_MAX_PRODUCTS = 100
SEMANTIC_MATCH_POOL_LIMIT = 20
CANDIDATE_POOL_LIMIT = SEMANTIC_MATCH_POOL_LIMIT
GPT_MATCH_CANDIDATE_LIMIT = 80
CUSTOMER_RESULT_LIMIT = 3
RERANK_SELECTION_LIMIT = 5  # legacy default; prefer rerank_selection_limit()
MAX_VARIANT_PRODUCT_QUERIES = 5

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def rerank_selection_limit() -> int:
    settings = get_settings()
    try:
        value = int(getattr(settings, "agent_rerank_selection_limit", 15) or 15)
    except (TypeError, ValueError):
        value = 15
    return max(5, min(20, value))


def revalidate_top_n() -> int:
    settings = get_settings()
    try:
        value = int(getattr(settings, "agent_revalidate_top_n", CUSTOMER_RESULT_LIMIT) or CUSTOMER_RESULT_LIMIT)
    except (TypeError, ValueError):
        value = CUSTOMER_RESULT_LIMIT
    return max(1, min(10, value))


def candidate_pool_limit() -> int:
    settings = get_settings()
    try:
        value = int(getattr(settings, "agent_candidate_pool_limit", CANDIDATE_POOL_LIMIT) or CANDIDATE_POOL_LIMIT)
    except (TypeError, ValueError):
        value = CANDIDATE_POOL_LIMIT
    return max(5, min(80, value))


class ProductRerankSelection(BaseModel):
    selected_product_ids: list[str]


class ProductMatchSelection(BaseModel):
    match_status: Literal["exact", "ambiguous", "none"]
    candidate_ids: list[str]
    best_candidate_id: str | None
    confidence: float


class ProductMatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpecificProductResolution:
    status: Literal["exact", "ambiguous", "none"]
    products: tuple[dict[str, Any], ...]
    match_source: Literal["exact", "openai"]
    invalid_ids_count: int = 0


@dataclass(frozen=True)
class CommercialPriceResolution:
    amount: Decimal | None
    source: str | None


@dataclass(frozen=True)
class ProductRetrievalRequest:
    strategy: str
    name: str | None = None
    brand: str | None = None
    reference: str | None = None
    ean: str | None = None
    category_id: str | None = None
    query: str | None = None
    tokens: tuple[str, ...] = ()
    available: bool | None = None
    available_in_store: bool | None = None
    limit: int = CANDIDATE_POOL_LIMIT
    page: int = 1

    def tool_arguments(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "query": self.query,
                "name": self.name,
                "brand": self.brand,
                "reference": self.reference,
                "ean": self.ean,
                "category_id": self.category_id,
                "tokens": list(self.tokens) if self.tokens else None,
                "available": self.available,
                "available_in_store": self.available_in_store,
                "limit": self.limit,
                "page": self.page,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class ProductRetrievalPlan:
    mode: Literal["exact", "recommendation"]
    requests: tuple[ProductRetrievalRequest, ...]
    candidate_limit: int = CANDIDATE_POOL_LIMIT
    customer_result_limit: int = CUSTOMER_RESULT_LIMIT
    discovery_page_limit: int = PRODUCT_PAGE_LIMIT
    discovery_max_pages: int = CATALOG_DISCOVERY_MAX_PAGES
    discovery_max_products: int = CATALOG_DISCOVERY_MAX_PRODUCTS


def _fold(value: Any) -> str:
    text = str(value or "")
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text).lower()
        if not unicodedata.combining(char)
    ).strip()


_MODEL_STOPWORDS = frozenset(
    {
        "relogio",
        "watch",
        "automatico",
        "automatic",
        "quartz",
        "cronografo",
        "chronograph",
        "mm",
        "com",
        "para",
        "the",
        "and",
    }
)
# Color/material adjectives are useful but must not block identity matches.
_OPTIONAL_MODEL_TOKENS = frozenset(
    {
        "branco",
        "preto",
        "rosa",
        "azul",
        "verde",
        "dourado",
        "prata",
        "cinza",
        "vermelho",
        "amarelo",
        "laranja",
        "bege",
        "claro",
        "escuro",
        "titanio",
        "aco",
        "ouro",
        "couro",
        "borracha",
        "carbon",
        "carbono",
        "ceramica",
        "nylon",
        "pulseira",
        "mostrador",
        "dial",
        "bezel",
    }
)
# Soft descriptors that must never block a match even for single-token models.
_DESCRIPTOR_MODEL_TOKENS = frozenset(
    {
        "claro",
        "escuro",
        "mostrador",
        "dial",
        "bezel",
        "face",
        "caixa",
        # Gender never participates in model identity / exact probes.
        "feminino",
        "feminina",
        "masculino",
        "masculina",
        "unissex",
        "unisex",
        "lady",
        "ladies",
        "dama",
        "damas",
        "women",
        "woman",
        "men",
        "man",
    }
)
# Strap/case materials from Vision — useful for ranking, never AND-required.
# Catalog titles usually only carry dial color (Branco/Rosa), not "pulseira bege".
_ACCESSORY_COLOR_TOKENS = frozenset(
    {
        "pulseira",
        "strap",
        "bracelet",
        "bege",
        "cream",
        "creme",
        "prata",
        "silver",
        "aco",
        "titanio",
        "couro",
        "leather",
        "borracha",
        "nylon",
        "ouro",
        "gold",
        "caixa",
        "case",
        "carcasa",
    }
)
_DIAL_COLOR_TOKENS = frozenset(
    {
        "branco",
        "preto",
        "rosa",
        "azul",
        "verde",
        "dourado",
        "cinza",
        "vermelho",
        "amarelo",
        "laranja",
        "pink",
        "blue",
        "black",
        "white",
        "green",
        "navy",
        "marinho",
        "red",
        "gold",
        "silver",
        "gray",
        "grey",
        "yellow",
        "orange",
    }
)
# PT ↔ EN (and close variants) so "azul" matches catalog "blue".
_COLOR_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"azul", "blue", "navy", "marinho"}),
    frozenset({"preto", "black"}),
    frozenset({"branco", "white"}),
    frozenset({"verde", "green"}),
    frozenset({"vermelho", "red"}),
    frozenset({"rosa", "pink", "rose"}),
    frozenset({"amarelo", "yellow"}),
    frozenset({"laranja", "orange"}),
    frozenset({"cinza", "gray", "grey"}),
    frozenset({"dourado", "gold", "golden"}),
    frozenset({"prata", "silver"}),
)
_ACCESSORY_NAME_TOKENS = frozenset(
    {
        "strap",
        "pulseira",
        "caixa",
        "box",
        "kit",
        "tool",
        "capa",
        "case",
        "fone",
        "cabo",
        "adapter",
        "adaptador",
    }
)
_REFERENCE_CODE_RE = re.compile(
    r"\b("
    r"[A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){2,}"
    r"|"
    r"[A-Z0-9]{2,}(?:\.[A-Z0-9]{2,}){2,}"
    r")\b",
    re.IGNORECASE,
)
# Alphanumeric model codes with digits: PH2000M, C63, SUB300, etc.
_MODEL_CODE_RE = re.compile(
    r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{3,}\b"
)


def significant_model_tokens(model: str | None) -> tuple[str, ...]:
    """Core tokens for exact matching — drops filler words like 'automático'."""
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", _fold(model))
        if token and token not in _MODEL_STOPWORDS and len(token) >= 2
    ]
    if tokens:
        return tuple(dict.fromkeys(tokens))
    fallback = [token for token in re.findall(r"[a-z0-9]+", _fold(model)) if token]
    return tuple(dict.fromkeys(fallback))


def required_model_tokens(model: str | None) -> tuple[str, ...]:
    """Identity tokens for matching; color/material are optional when possible."""
    tokens = significant_model_tokens(model)
    identity = tuple(
        token for token in tokens if token not in _OPTIONAL_MODEL_TOKENS
    )
    if len(identity) >= 2 or any(re.search(r"\d", token) for token in identity):
        return identity
    # For single-word models (Sealander), keep color tokens for ranking/disambiguation
    # but never require dial descriptors invented by Vision ("claro", "mostrador").
    tightened = tuple(
        token for token in tokens if token not in _DESCRIPTOR_MODEL_TOKENS
    )
    return tightened or identity or tokens


def identity_core_tokens(
    model: str | None,
    *,
    color_tokens: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Model tokens for Tray probes — never mix hue words into the core query."""
    required = required_model_tokens(model)
    drop = set(color_tokens) | _OPTIONAL_MODEL_TOKENS | _DESCRIPTOR_MODEL_TOKENS
    core = tuple(token for token in required if token not in drop)
    if core:
        return core
    # Fall back to non-color identity even for single-token models.
    identity = tuple(
        token
        for token in significant_model_tokens(model)
        if token not in drop
    )
    return identity or required


def _rejects_as_accessory(
    product: dict[str, Any],
    identity_tokens: tuple[str, ...],
) -> bool:
    """Single-token model asks must not match straps/kits."""
    if len(identity_tokens) != 1:
        return False
    candidate_model = _fold(product.get("model"))
    candidate_name = _fold(product.get("name"))
    name_tokens = set(re.findall(r"[a-z0-9]+", candidate_name))
    if candidate_model:
        return False
    return bool(name_tokens & _ACCESSORY_NAME_TOKENS)


def score_catalog_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    require_color: bool = False,
    allow_movement_mismatch: bool = False,
    limit: int = RERANK_SELECTION_LIMIT,
) -> list[dict[str, Any]]:
    """Rank catalog rows by brand/model/color keyword overlap in the title."""
    color_tokens = preference_color_tokens(interpretation)
    feature_tokens = preference_feature_tokens(interpretation)
    case_tokens = preference_case_finish_tokens(interpretation)
    identity_tokens = identity_core_tokens(
        interpretation.subject.model,
        color_tokens=color_tokens,
    )
    model_codes = {
        _fold(code)
        for code in extract_model_codes(interpretation.subject.model)
    }
    brand_fold = _fold(interpretation.subject.brand)
    scored: list[tuple[int, dict[str, Any]]] = []
    for product in products:
        text = _product_text(product)
        candidate_brand = _fold(product.get("brand"))
        if brand_fold:
            if candidate_brand and candidate_brand != brand_fold:
                continue
            if not candidate_brand and brand_fold not in text:
                continue
        if identity_tokens and not all(token in text for token in identity_tokens):
            continue
        if _rejects_as_accessory(product, identity_tokens):
            continue
        if feature_tokens and not product_matches_feature_tokens(product, feature_tokens):
            continue
        movement_ok = product_compatible_with_requested_movement(
            product,
            interpretation.subject.model,
            interpretation.preferences.attributes,
        )
        if not movement_ok and not allow_movement_mismatch:
            continue
        color_ok = (
            not color_tokens
            or product_matches_color_tokens(product, color_tokens)
        )
        if require_color and color_tokens and not color_ok:
            continue
        score = 0
        score += 20 * len(identity_tokens)
        if model_codes and any(code in text for code in model_codes):
            score += 50
        if color_ok and color_tokens:
            score += 30
        if feature_tokens and product_matches_feature_tokens(product, feature_tokens):
            score += 40
        if case_tokens:
            # Soft: silver/steel case should outrank all-black-case siblings when
            # dial color alone collides on "preto".
            if any(token in text for token in case_tokens):
                score += 15
            elif {"prata", "aco", "steel"} & set(case_tokens) and "preto" in text:
                # Title says Preto (often dial) but finish asked for steel —
                # mild penalty vs Samurai/steel-titled siblings.
                score -= 10
        if movement_ok:
            score += 10
        elif model_excludes_gmt(interpretation.subject.model) and "gmt" in text:
            score -= 5
        if any(
            product.get(key) not in (None, "", 0, "0")
            for key in ("current_price", "promotional_price", "price")
        ):
            score += 5
        scored.append((score, product))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    return [product for _, product in scored[: max(1, limit)]]


def keyword_match_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    require_color: bool = False,
) -> list[dict[str, Any]]:
    """Compatibility wrapper around score_catalog_candidates."""
    return score_catalog_candidates(
        products,
        interpretation,
        require_color=require_color,
        allow_movement_mismatch=False,
        limit=max(RERANK_SELECTION_LIMIT, CUSTOMER_RESULT_LIMIT),
    )


def is_plausible_product_reference(value: str | None) -> bool:
    """True when value looks like a commercial SKU/ref, not a color description."""
    text = str(value or "").strip()
    if not text:
        return False
    if extract_reference_code(text):
        return True
    folded = _fold(text)
    tokens = [token for token in re.findall(r"[a-z0-9]+", folded) if token]
    if not tokens:
        return False
    soft = _OPTIONAL_MODEL_TOKENS | _DESCRIPTOR_MODEL_TOKENS | _MODEL_STOPWORDS
    if all(token in soft for token in tokens):
        return False
    # Real refs almost always mix letters/digits with separators.
    if re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", text) and re.search(
        r"[\.\-_/]",
        text,
    ):
        return len(text) >= 6
    return False


def effective_product_reference(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if is_plausible_product_reference(text):
        return extract_reference_code(text) or text
    return None


def normalize_pt_catalog_query(text: str | None) -> str:
    """Map common English vision terms to Tray catalog Portuguese."""
    value = str(text or "").strip()
    if not value:
        return ""
    replacements = (
        (r"\bautomatic\b", "Automático"),
        (r"\bchronograph\b", "Cronógrafo"),
        (r"\bquartz\b", "Quartz"),
        (r"\bpink\b", "Rosa"),
        (r"\bblue\b", "Azul"),
        (r"\bgreen\b", "Verde"),
        (r"\bblack\b", "Preto"),
        (r"\bwhite\b", "Branco"),
    )
    updated = value
    for pattern, replacement in replacements:
        updated = re.sub(pattern, replacement, updated, flags=re.IGNORECASE)
    return updated


def expand_color_aliases(token: str | None) -> frozenset[str]:
    """Expand a color word to PT/EN synonyms used in Tray titles."""
    folded = _fold(token)
    if not folded:
        return frozenset()
    for group in _COLOR_ALIAS_GROUPS:
        if folded in group:
            return group
    return frozenset({folded})


def preference_color_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """Dial-color tokens only — never strap/case materials from Vision dumps."""
    color = _fold(interpretation.preferences.color)
    if not color:
        # Recover color adjectives embedded in the model string from Vision.
        color = " ".join(
            token
            for token in significant_model_tokens(interpretation.subject.model)
            if token in _OPTIONAL_MODEL_TOKENS
            and token not in _DESCRIPTOR_MODEL_TOKENS
            and token not in _ACCESSORY_COLOR_TOKENS
        )
    raw = [
        token
        for token in re.findall(r"[a-z0-9]+", color)
        if token
        and token not in _DESCRIPTOR_MODEL_TOKENS
        and token not in _ACCESSORY_COLOR_TOKENS
    ]
    # Prefer known dial hues; if Vision only sent accessories, require nothing.
    dial = [token for token in raw if token in _DIAL_COLOR_TOKENS]
    if dial:
        # One dial hue is enough for AND/require_color (branco, not branco+bege).
        return (dial[0],)
    return ()


def preference_color_search_labels(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """All alias spellings to probe Tray name filters (azul → azul, blue, …)."""
    tokens = preference_color_tokens(interpretation)
    if not tokens:
        return ()
    labels: list[str] = []
    for token in tokens:
        for alias in sorted(expand_color_aliases(token)):
            labels.append(alias)
    return tuple(dict.fromkeys(labels))

def catalog_match_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """Significant AND-search tokens for Tray token/ILIKE lookup."""
    subject = interpretation.subject
    tokens: list[str] = []
    for part in (subject.brand or "").split():
        folded = _fold(part)
        if folded and len(folded) >= 2:
            tokens.append(folded)
    color_tokens = preference_color_tokens(interpretation)
    # Strip accessory words from model before core identity (Vision dumps).
    model_for_core = " ".join(
        token
        for token in significant_model_tokens(subject.model)
        if token not in _ACCESSORY_COLOR_TOKENS
        and token not in _DESCRIPTOR_MODEL_TOKENS
    ) or subject.model
    core = identity_core_tokens(model_for_core, color_tokens=color_tokens)
    tokens.extend(core)
    for code in extract_model_codes(subject.model):
        tokens.append(_fold(code))
    tokens.extend(color_tokens)
    # Keep movement when Vision asked Automatic — helps avoid GMT substitutes.
    if model_excludes_gmt(subject.model):
        tokens.append("automatico")
    # Drop ultra-generic fillers that drown AND matches.
    drop = {"relogio", "watch", "mm"} | _ACCESSORY_COLOR_TOKENS
    cleaned = [
        token
        for token in dict.fromkeys(tokens)
        if token and token not in drop and token not in _DESCRIPTOR_MODEL_TOKENS
    ]
    return tuple(cleaned)


_FEATURE_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "cronografo": ("cronografo", "chronograph", "chrono"),
    "mergulho": ("mergulho", "diver", "divers", "200m"),
    "gmt": ("gmt",),
}


def preference_feature_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """Distinctive function tokens from preferences.attributes (photo Vision)."""
    tokens: list[str] = []
    for item in interpretation.preferences.attributes or []:
        folded = _fold(item)
        if not folded:
            continue
        if "crono" in folded or "chrono" in folded:
            tokens.append("cronografo")
        elif "diver" in folded or "mergulho" in folded or "200m" in folded:
            tokens.append("mergulho")
        elif folded == "gmt":
            tokens.append("gmt")
    return tuple(dict.fromkeys(tokens))


def preference_gender_tokens(interpretation: SalesInterpretation) -> tuple[str, ...]:
    """Catalog search/ranking tokens for requested gender (soft, not AND-hard)."""
    from .preference_normalize import gender_search_aliases, preference_gender_label

    return gender_search_aliases(preference_gender_label(interpretation))


def product_matches_gender_tokens(
    product: dict[str, Any],
    gender_tokens: tuple[str, ...],
) -> bool:
    if not gender_tokens:
        return True
    text = _product_text(product)
    return any(token in text for token in gender_tokens)


def product_matches_feature_tokens(
    product: dict[str, Any],
    feature_tokens: tuple[str, ...],
) -> bool:
    if not feature_tokens:
        return True
    text = _product_text(product)
    for token in feature_tokens:
        aliases = _FEATURE_SEARCH_ALIASES.get(token, (token,))
        if not any(alias in text for alias in aliases):
            return False
    return True


def preference_case_finish_tokens(
    interpretation: SalesInterpretation,
) -> tuple[str, ...]:
    """Soft case/bracelet finish cues (never AND-required alone)."""
    material = _fold(interpretation.preferences.material)
    if not material:
        return ()
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", material)
        if token and token not in _DESCRIPTOR_MODEL_TOKENS
    ]
    # Map common Vision finishes to catalog-ish tokens.
    expanded: list[str] = []
    for token in tokens:
        if token in {"prata", "silver", "aco", "steel", "inox"}:
            expanded.extend(["prata", "aco", "steel"])
        elif token in {"preto", "black", "ion", "pvd"}:
            expanded.extend(["preto", "black"])
        else:
            expanded.append(token)
    return tuple(dict.fromkeys(expanded))


def product_matches_color_tokens(
    product: dict[str, Any],
    color_tokens: tuple[str, ...],
) -> bool:
    if not color_tokens:
        return True
    text = _product_text(product)
    for token in color_tokens:
        aliases = expand_color_aliases(token)
        if not any(alias in text for alias in aliases):
            return False
    return True


def model_excludes_gmt(model: str | None) -> bool:
    folded = _fold(model)
    if not folded:
        return False
    if "gmt" in folded:
        return False
    return "automatic" in folded or "automatico" in folded


def requests_automatic_movement(
    model: str | None,
    attributes: list[str] | None = None,
) -> bool:
    blob = _fold(
        " ".join(
            part
            for part in ((model or ""), *(attributes or ()))
            if part
        )
    )
    return "automatic" in blob or "automatico" in blob


def product_compatible_with_requested_movement(
    product: dict[str, Any],
    model: str | None,
    attributes: list[str] | None = None,
) -> bool:
    text = _product_text(product)
    wants_auto = requests_automatic_movement(model, attributes)
    if wants_auto:
        # Don't substitute GMT siblings for a plain Automatic ask.
        if "gmt" in text and "gmt" not in _fold(model):
            return False
        mechanical = (
            "mecanico" in text
            or "mechanical" in text
            or bool(re.search(r"\bh\s*mecan", text))
        )
        has_auto = "automatico" in text or "automatic" in text
        if mechanical and not has_auto:
            return False
    elif model_excludes_gmt(model):
        return "gmt" not in text
    return True


def extract_reference_code(text: str | None) -> str | None:
    match = _REFERENCE_CODE_RE.search(str(text or ""))
    return match.group(1).strip() if match else None


def extract_model_codes(text: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_MODEL_CODE_RE.findall(str(text or ""))))


def _product_text(product: dict[str, Any]) -> str:
    fields = (
        "name", "brand", "model", "reference", "ean", "description",
        "category", "category_name", "category_id", "attributes", "color",
        "style", "material", "properties", "ProductSettings", "variants",
    )
    return _fold(" ".join(str(product.get(field) or "") for field in fields))


def specific_product_search_terms(
    interpretation: SalesInterpretation,
) -> tuple[str, ...]:
    subject = interpretation.subject
    preferences = interpretation.preferences
    values = (
        subject.model,
        subject.product_type,
        preferences.style,
        preferences.color,
        preferences.material,
        *preferences.attributes,
    )
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _fold(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
    return tuple(terms)


def _term_tokens(terms: tuple[str, ...]) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for term in terms:
        for token in re.findall(r"[a-z0-9]+", term):
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)


def _evenly_spaced_candidates(
    products: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if len(products) <= limit:
        return products
    if limit <= 1:
        return products[:limit]
    indexes = {
        round(index * (len(products) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [products[index] for index in sorted(indexes)]


def prefilter_specific_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = SEMANTIC_MATCH_POOL_LIMIT,
) -> list[dict[str, Any]]:
    """Reduce real catalog candidates without deciding semantic correctness."""
    terms = specific_product_search_terms(interpretation)
    tokens = _term_tokens(terms)
    scored: list[tuple[int, int, int, dict[str, Any]]] = []
    unscored: list[dict[str, Any]] = []
    for index, product in enumerate(products):
        text = _product_text(product)
        phrase_matches = sum(1 for term in terms if term in text)
        token_matches = sum(1 for token in tokens if token in text)
        if phrase_matches or token_matches:
            scored.append((phrase_matches, token_matches, index, product))
        else:
            unscored.append(product)
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    selected = [product for _, _, _, product in scored[:limit]]
    if len(selected) < limit:
        selected_ids = {
            str(product.get("id"))
            for product in selected
            if product.get("id") is not None
        }
        remaining = [
            product
            for product in unscored
            if product.get("id") is None
            or str(product.get("id")) not in selected_ids
        ]
        selected.extend(
            _evenly_spaced_candidates(remaining, limit - len(selected))
        )
    return selected[:limit]


class ProductRetrievalCompiler:
    @staticmethod
    def compile(
        interpretation: SalesInterpretation,
        *,
        category_ids: tuple[str, ...] | list[str] = (),
    ) -> ProductRetrievalPlan:
        subject = interpretation.subject
        from .preference_normalize import is_gender_only_label

        model_for_exact = None if is_gender_only_label(subject.model) else subject.model
        inferred_reference = effective_product_reference(subject.reference) or (
            extract_reference_code(
                " ".join(
                    part
                    for part in (model_for_exact, subject.brand, subject.product_type)
                    if part
                )
            )
        )
        exact = bool(inferred_reference or subject.ean or model_for_exact)
        requests: list[ProductRetrievalRequest] = []

        if subject.ean:
            requests.append(ProductRetrievalRequest(strategy="exact_ean", ean=subject.ean))
        elif inferred_reference:
            requests.append(
                ProductRetrievalRequest(
                    strategy="exact_reference",
                    reference=inferred_reference,
                )
            )
            if model_for_exact:
                requests.append(
                    ProductRetrievalRequest(
                        strategy="exact_query_reference_context",
                        query=" ".join(
                            part
                            for part in (subject.brand, model_for_exact)
                            if part
                        ).strip(),
                    )
                )
        elif model_for_exact:
            pt_model = normalize_pt_catalog_query(model_for_exact)
            color_tokens = preference_color_tokens(interpretation)
            color_label = " ".join(color_tokens).strip()
            core_tokens = identity_core_tokens(
                pt_model or subject.model,
                color_tokens=color_tokens,
            )
            core_query = " ".join(core_tokens[:4]).strip()
            core_label = core_query.title() if core_query else ""
            model_codes = extract_model_codes(pt_model or subject.model)
            wants_automatic = bool(
                re.search(r"\b(automatic|automatico)\b", _fold(subject.model))
            )
            auto_bit = "Automático" if wants_automatic else None
            color_hue = color_label.title() if color_label else None
            seen_probe_keys: set[str] = set()
            # Extra slot when dial color is known so short family+color and the
            # catalog-title probe can both run without dropping brand query.
            tier1_budget = 7 if color_hue else 6
            match_tokens = catalog_match_tokens(interpretation)
            if match_tokens:
                requests.append(
                    ProductRetrievalRequest(
                        strategy="token_and_search",
                        brand=subject.brand,
                        tokens=match_tokens,
                        limit=PRODUCT_PAGE_LIMIT,
                    )
                )

            def _add_probe(
                strategy: str,
                *,
                name: str | None = None,
                brand: str | None = None,
                query: str | None = None,
            ) -> None:
                nonlocal tier1_budget
                if tier1_budget <= 0:
                    return
                cleaned_name = " ".join(str(name or "").split()).strip() or None
                cleaned_query = " ".join(str(query or "").split()).strip() or None
                if not cleaned_name and not cleaned_query:
                    return
                # Phrase already contains the brand → do not also filter by brand.
                brand_filter = brand
                if (
                    brand_filter
                    and cleaned_name
                    and _fold(brand_filter) in _fold(cleaned_name)
                ):
                    brand_filter = None
                key = (
                    f"name|{_fold(cleaned_name)}|"
                    f"query|{_fold(cleaned_query)}|"
                    f"brand|{_fold(brand_filter)}"
                )
                if key in seen_probe_keys:
                    return
                seen_probe_keys.add(key)
                requests.append(
                    ProductRetrievalRequest(
                        strategy=strategy,
                        name=cleaned_name,
                        brand=brand_filter,
                        query=cleaned_query,
                    )
                )
                tier1_budget -= 1

            # Tier 1 — at most 6 high-signal probes (no brand paging here).
            if model_codes:
                _add_probe(
                    "exact_model_code",
                    name=model_codes[0],
                    brand=subject.brand,
                )
            identity_name = (
                " ".join(
                    part
                    for part in (core_label or None, auto_bit if color_hue else None)
                    if part
                ).strip()
                or (pt_model or subject.model)
            )
            _add_probe(
                "exact_model_with_brand" if subject.brand else "exact_model",
                name=identity_name if color_hue else (pt_model or subject.model),
                brand=subject.brand,
            )
            if color_hue and core_label:
                _add_probe(
                    "exact_color_core",
                    name=f"{core_label} {color_hue}".strip(),
                    brand=subject.brand,
                )
                if auto_bit:
                    _add_probe(
                        "exact_color_automatic",
                        name=f"{core_label} {auto_bit} {color_hue}".strip(),
                        brand=subject.brand,
                    )
            # Short family+color beats long titles on Tray's name filter.
            if color_hue and model_codes:
                _add_probe(
                    "exact_color_family_code",
                    name=f"{model_codes[0]} {color_hue}".strip(),
                    brand=subject.brand,
                )
            catalog_title = " ".join(
                part
                for part in (
                    "Relógio",
                    subject.brand,
                    core_label or None,
                    auto_bit,
                    color_hue,
                )
                if part
            ).strip()
            if catalog_title:
                _add_probe("exact_catalog_title", name=catalog_title)
            brand_model_query = " ".join(
                part for part in (subject.brand, core_label or identity_name) if part
            ).strip()
            if brand_model_query and _fold(brand_model_query) != _fold(
                identity_name or ""
            ):
                _add_probe("exact_query_full", query=brand_model_query)
            # Tier 2 hooks — executed only when Tier 1 matching misses.
            if subject.brand:
                requests.append(ProductRetrievalRequest(
                    strategy="brand_candidates",
                    brand=subject.brand,
                ))
            elif category_ids:
                for category_id in category_ids[:5]:
                    requests.append(ProductRetrievalRequest(
                        strategy="category_candidates",
                        category_id=str(category_id),
                    ))
        else:
            available = True
            available_in_store = True
            for index, category_id in enumerate(category_ids[:5]):
                requests.append(ProductRetrievalRequest(
                    strategy="category" if index == 0 else "category_child",
                    category_id=str(category_id),
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))
            if subject.product_type:
                gender_tokens = preference_gender_tokens(interpretation)
                gender_label = gender_tokens[0] if gender_tokens else None
                # Prefer gendered catalog query ("relógio feminino") so Tray
                # surfaces the right segment before soft ranking.
                primary_name = (
                    f"{subject.product_type} {gender_label}".strip()
                    if gender_label
                    else subject.product_type
                )
                requests.append(ProductRetrievalRequest(
                    strategy="name_fallback",
                    name=primary_name,
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))
                if gender_label and primary_name != subject.product_type:
                    requests.append(ProductRetrievalRequest(
                        strategy="name_fallback_category",
                        name=subject.product_type,
                        brand=subject.brand,
                        available=available,
                        available_in_store=available_in_store,
                    ))
            elif subject.brand:
                requests.append(ProductRetrievalRequest(
                    strategy="explicit_brand",
                    brand=subject.brand,
                    available=available,
                    available_in_store=available_in_store,
                ))

            # Color probes (PT/EN aliases) so Tray returns blue when user said azul.
            color_labels = preference_color_search_labels(interpretation)
            if subject.brand and color_labels:
                for label in color_labels[:4]:
                    requests.append(ProductRetrievalRequest(
                        strategy="color_brand_probe",
                        name=label,
                        brand=subject.brand,
                        available=available,
                        available_in_store=available_in_store,
                    ))
            elif color_labels and subject.product_type:
                for label in color_labels[:3]:
                    requests.append(ProductRetrievalRequest(
                        strategy="color_name_probe",
                        name=f"{subject.product_type} {label}".strip(),
                        brand=subject.brand,
                        available=available,
                        available_in_store=available_in_store,
                    ))

        discovery_pages = CATALOG_DISCOVERY_MAX_PAGES
        if preference_color_tokens(interpretation):
            # A few extra brand pages help surface color variants without
            # blowing the Vercel Hobby wall-clock budget.
            discovery_pages = max(discovery_pages, 8)

        return ProductRetrievalPlan(
            mode="exact" if exact else "recommendation",
            requests=tuple(requests),
            discovery_max_pages=discovery_pages,
            discovery_max_products=max(
                CATALOG_DISCOVERY_MAX_PRODUCTS,
                discovery_pages * PRODUCT_PAGE_LIMIT,
            ),
        )


def _decimal_money_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("value", "amount"):
            if key in value:
                return _decimal_money_value(value.get(key))
        return None
    try:
        if isinstance(value, str):
            normalized = value.replace("R$", "").replace("\xa0", " ").strip()
            normalized = normalized.replace(" ", "")
            if "," in normalized:
                normalized = normalized.replace(".", "").replace(",", ".")
            return Decimal(normalized)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def resolve_commercial_price(
    product: dict[str, Any],
    *,
    require_positive: bool = False,
) -> CommercialPriceResolution:
    first_present_source: str | None = None
    for key in ("current_price", "promotional_price", "price"):
        if key not in product or product.get(key) is None:
            continue
        first_present_source = first_present_source or key
        amount = _decimal_money_value(product.get(key))
        if require_positive and (amount is None or amount <= Decimal("0")):
            continue
        return CommercialPriceResolution(amount=amount, source=key)
    return CommercialPriceResolution(amount=None, source=first_present_source)


def effective_price(product: dict[str, Any]) -> float | None:
    resolved = resolve_commercial_price(product)
    return float(resolved.amount) if resolved.amount is not None else None


def _known_unavailable(product: dict[str, Any]) -> bool:
    availability_fields = (
        product.get("available"),
        product.get("available_in_store"),
        product.get("available_for_purchase"),
    )
    known = [value for value in availability_fields if value is not None]
    if not known:
        return False
    def is_false(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"0", "false", "no", "não"}
        return value is False or value == 0
    return all(is_false(value) for value in known)


def _truth_state(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = _fold(value)
        if normalized in {"1", "true", "yes", "sim"}:
            return True
        if normalized in {"0", "false", "no", "nao"}:
            return False
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return None


def product_availability_state(
    product: dict[str, Any],
) -> Literal["available", "unavailable", "unknown"]:
    for source in (product, product.get("ProductSettings")):
        if not isinstance(source, dict):
            continue
        if _truth_state(source.get("upon_request")) is True:
            return "unavailable"
    values: list[bool] = []
    for key in ("available", "available_in_store", "available_for_purchase"):
        state = _truth_state(product.get(key))
        if state is not None:
            values.append(state)
    settings = product.get("ProductSettings")
    if isinstance(settings, dict):
        for key in ("available", "available_in_store", "available_for_purchase"):
            state = _truth_state(settings.get(key))
            if state is not None:
                values.append(state)
    variants = product.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            for key in ("available", "available_in_store", "available_for_purchase"):
                state = _truth_state(variant.get(key))
                if state is not None:
                    values.append(state)
            variant_settings = variant.get("VariationSettings")
            if isinstance(variant_settings, dict):
                for key in ("available", "available_in_store", "available_for_purchase"):
                    state = _truth_state(variant_settings.get(key))
                    if state is not None:
                        values.append(state)
    if any(values):
        return "available"
    if values and not any(values):
        return "unavailable"
    return "unknown"


def commercial_availability_facts(product: dict[str, Any]) -> dict[str, Any]:
    settings = product.get("ProductSettings")
    settings = settings if isinstance(settings, dict) else {}
    lead_time_value = next(
        (
            source.get(key)
            for source in (product, settings)
            for key in (
                "order_days_availability",
                "lead_time_days",
                "availability_days",
                "delivery_days",
                "lead_time",
            )
            if source.get(key) not in (None, "")
        ),
        None,
    )
    lead_time_days: int | None = None
    if isinstance(lead_time_value, (int, float)) and not isinstance(lead_time_value, bool):
        lead_time_days = max(int(lead_time_value), 0)
    elif isinstance(lead_time_value, str):
        match = re.search(r"\d+", lead_time_value)
        if match:
            lead_time_days = int(match.group(0))

    immediate_flag = next(
        (
            state
            for source in (product, settings)
            for key in ("immediate_delivery", "ready_to_ship")
            if (state := _truth_state(source.get(key))) is not None
        ),
        None,
    )
    if lead_time_days is not None:
        immediate_delivery_supported: bool | None = lead_time_days == 0
    else:
        immediate_delivery_supported = immediate_flag
    stock = product.get("stock")
    return {
        "availability_state": product_availability_state(product),
        "has_stock": stock is not None,
        "stock": stock,
        "has_lead_time": lead_time_days is not None,
        "lead_time_days": lead_time_days,
        "immediate_delivery_supported": immediate_delivery_supported,
    }


def hard_filter_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    mode: Literal["exact", "recommendation"],
) -> list[dict[str, Any]]:
    """Apply mandatory filters. Prefer TurnUnderstanding hard constraints when present."""
    subject = interpretation.subject
    preferences = interpretation.preferences
    expected_brand = _fold(subject.brand)
    expected_model = _fold(subject.model)
    expected_reference = _fold(effective_product_reference(subject.reference))
    expected_ean = _fold(subject.ean)
    brand_exclusive = False
    exact_only = False
    hard_color = None
    hard_material = None
    try:
        from .catalog_index import _hard_constraints_from_interpretation

        hard = _hard_constraints_from_interpretation(interpretation)
        expected_brand = _fold(hard.get("brand")) or expected_brand
        expected_reference = _fold(hard.get("reference")) or expected_reference
        expected_ean = _fold(hard.get("ean")) or expected_ean
        brand_exclusive = bool(hard.get("brand_exclusive"))
        exact_only = bool(hard.get("exact_only"))
        hard_color = _fold(hard.get("dial_color"))
        hard_material = _fold(hard.get("material"))
        if hard.get("budget_max") is not None:
            preferences = preferences.model_copy(
                update={"budget_max": hard.get("budget_max")}
            )
        if hard.get("budget_min") is not None:
            preferences = preferences.model_copy(
                update={"budget_min": hard.get("budget_min")}
            )
    except Exception:
        pass

    selected: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict) or not product.get("id"):
            continue
        text = _product_text(product)
        if expected_brand:
            candidate_brand = _fold(product.get("brand"))
            if candidate_brand and candidate_brand != expected_brand:
                continue
            if not candidate_brand and expected_brand not in text:
                continue
            if brand_exclusive and candidate_brand and candidate_brand != expected_brand:
                continue
        if expected_reference and _fold(product.get("reference")) != expected_reference:
            continue
        if expected_ean and _fold(product.get("ean")) != expected_ean:
            continue
        if not product_compatible_with_requested_movement(
            product,
            subject.model,
            interpretation.preferences.attributes,
        ):
            continue
        color_tokens = preference_color_tokens(interpretation)
        if hard_color:
            color_tokens = tuple(dict.fromkeys((*color_tokens, hard_color)))
        # Exact identity searches still require color evidence (with aliases).
        # Recommendation keeps brand/category pool intact so the LLM/reranker
        # can match "azul" ↔ "blue" — unless the customer said "somente"/exact_only.
        require_color = mode == "exact" or (exact_only and bool(color_tokens or hard_color))
        if (
            require_color
            and color_tokens
            and not product_matches_color_tokens(product, color_tokens)
        ):
            continue
        if hard_material and exact_only:
            if hard_material not in text and hard_material not in _fold(product.get("material")):
                continue
        if mode == "exact" and expected_model:
            model_tokens = list(required_model_tokens(subject.model))
            if model_tokens and not all(token in text for token in model_tokens):
                continue
        price = effective_price(product)
        if preferences.budget_min is not None and (price is None or price < preferences.budget_min):
            continue
        if preferences.budget_max is not None and (price is None or price > preferences.budget_max):
            continue
        if mode == "recommendation" and _known_unavailable(product):
            continue
        selected.append(product)
    return selected


def semantic_preferences(interpretation: SalesInterpretation) -> dict[str, Any]:
    preferences = interpretation.preferences
    gender = None
    try:
        from .preference_normalize import preference_gender_label

        gender = preference_gender_label(interpretation)
    except Exception:
        gender = preferences.recipient
    return {
        key: value
        for key, value in {
            "style": preferences.style,
            "color": preferences.color,
            "material": preferences.material,
            "occasion": preferences.occasion,
            "recipient": preferences.recipient,
            "gender": gender,
            "attributes": preferences.attributes,
            "explicit_no_preferences": preferences.explicit_no_preferences,
        }.items()
        if value not in (None, [], "")
    }


def _compact_property_evidence(value: Any, *, depth: int = 0) -> Any:
    if depth >= 2:
        return str(value)[:160]
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [
            _compact_property_evidence(item, depth=depth + 1)
            for item in value[:12]
        ]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _compact_property_evidence(item, depth=depth + 1)
            for key, item in list(value.items())[:20]
        }
    return str(value)[:160]


def compact_candidates(
    products: list[dict[str, Any]],
    *,
    limit: int = CANDIDATE_POOL_LIMIT,
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for product in products[: max(1, limit)]:
        compact.append({
            key: value
            for key, value in {
                "id": str(product.get("id")) if product.get("id") is not None else None,
                "name": product.get("name"),
                "brand": product.get("brand"),
                "model": product.get("model"),
                "category": product.get("category"),
                "category_name": product.get("category_name"),
                "category_id": product.get("category_id"),
                "description": str(product.get("description") or "")[:240] or None,
                "properties": _compact_property_evidence(
                    product.get("properties") or product.get("attributes")
                ),
                "color": product.get("color"),
                "style": product.get("style"),
                "material": product.get("material"),
                "price": product.get("price"),
                "promotional_price": product.get("promotional_price"),
                "current_price": product.get("current_price"),
                "availability": product.get("availability"),
                "available": product.get("available"),
                "available_in_store": product.get("available_in_store"),
                "has_variation": product.get("has_variation"),
                "ProductSettings": product.get("ProductSettings"),
                "variants": _compact_variants(product.get("variants")),
            }.items()
            if value is not None
        })
    return compact


def _brand_compatible_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    expected_brand = _fold(interpretation.subject.brand)
    if not expected_brand:
        return list(products)
    compatible: list[dict[str, Any]] = []
    for product in products:
        candidate_brand = _fold(product.get("brand"))
        if candidate_brand and candidate_brand == expected_brand:
            compatible.append(product)
            continue
        if not candidate_brand and expected_brand in _product_text(product):
            compatible.append(product)
    return compatible


def infer_family_codes_from_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> tuple[str, ...]:
    """Pull shared family codes (C63, C60…) from sibling titles already in the pool."""
    color_tokens = preference_color_tokens(interpretation)
    identity_tokens = identity_core_tokens(
        interpretation.subject.model,
        color_tokens=color_tokens,
    )
    if not identity_tokens:
        return ()
    codes: list[str] = []
    for product in products:
        text = _product_text(product)
        if not all(token in text for token in identity_tokens):
            continue
        name = str(product.get("name") or "")
        # Prefer short family prefixes (C63). Ignore reference fragments like
        # 39AGM3 that pollute Tray name probes and burn the enrich budget.
        for match in re.findall(r"\b[Cc]\d{2}\b", name):
            codes.append(match.upper())
        for code in extract_model_codes(name):
            if re.fullmatch(r"[Cc]\d{2}", code):
                codes.append(code.upper())
    return tuple(dict.fromkeys(codes))[:3]


def soft_confirm_candidates(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    *,
    limit: int = CUSTOMER_RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """Best catalog near-matches to show for 'é esse da foto?' confirmation."""
    color_tokens = preference_color_tokens(interpretation)
    if color_tokens:
        # Never substitute Kingfisher/Dagger when a dial color was requested.
        return score_catalog_candidates(
            products,
            interpretation,
            require_color=True,
            allow_movement_mismatch=False,
            limit=limit,
        )
    identity_hits = score_catalog_candidates(
        products,
        interpretation,
        require_color=False,
        allow_movement_mismatch=False,
        limit=limit,
    )
    if identity_hits:
        return identity_hits
    # Last resort: allow GMT siblings only when nothing movement-compatible exists.
    return score_catalog_candidates(
        products,
        interpretation,
        require_color=False,
        allow_movement_mismatch=True,
        limit=limit,
    )


def exact_progress_matches(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    """Matches good enough to stop searching — honors color when requested."""
    color_tokens = preference_color_tokens(interpretation)
    return score_catalog_candidates(
        products,
        interpretation,
        require_color=bool(color_tokens),
        allow_movement_mismatch=False,
        limit=RERANK_SELECTION_LIMIT,
    )


def exact_specific_product_matches(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    subject = interpretation.subject
    candidates = _brand_compatible_candidates(products, interpretation)
    expected_reference = _fold(effective_product_reference(subject.reference))
    expected_ean = _fold(subject.ean)
    expected_model = _fold(subject.model)
    expected_brand_model = _fold(
        " ".join(
            value
            for value in (subject.brand, subject.model)
            if value
        )
    )
    matches: list[dict[str, Any]] = []
    for product in candidates:
        if not product_compatible_with_requested_movement(
            product,
            subject.model,
            interpretation.preferences.attributes,
        ):
            continue
        if expected_reference:
            if _fold(product.get("reference")) == expected_reference:
                matches.append(product)
            continue
        if expected_ean:
            if _fold(product.get("ean")) == expected_ean:
                matches.append(product)
            continue
        if expected_model:
            candidate_model = _fold(product.get("model"))
            candidate_name = _fold(product.get("name"))
            if candidate_model == expected_model or candidate_name in {
                expected_model,
                expected_brand_model,
            }:
                matches.append(product)
                continue
            # Tray often stores short model ("Sealander") while the customer
            # asks with style/color words ("C63 Sealander Automático Rosa").
            # Color/material tokens are optional when identity tokens suffice.
            required = required_model_tokens(subject.model)
            text = _product_text(product)
            if not required or not all(token in text for token in required):
                continue
            if len(required) >= 2:
                matches.append(product)
                continue
            token = required[0]
            if candidate_model in {token, expected_model}:
                matches.append(product)
                continue
            if candidate_model and token not in candidate_model:
                continue
            name_tokens = set(re.findall(r"[a-z0-9]+", candidate_name))
            # Single-token asks ("Explorer") must not match accessories
            # like "Explorer Strap" when the model field is empty.
            if not candidate_model and name_tokens & _ACCESSORY_NAME_TOKENS:
                continue
            if token in candidate_name:
                matches.append(product)
    return matches


async def match_specific_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> SpecificProductResolution:
    compatible = _brand_compatible_candidates(products, interpretation)
    color_tokens = preference_color_tokens(interpretation)
    # Local keyword score first — works for any brand/title shape.
    scored = score_catalog_candidates(
        compatible,
        interpretation,
        require_color=bool(color_tokens),
        allow_movement_mismatch=False,
        limit=RERANK_SELECTION_LIMIT,
    )
    if scored:
        selected = scored[:RERANK_SELECTION_LIMIT]
        status: Literal["exact", "ambiguous", "none"] = (
            "exact" if len(selected) == 1 else "ambiguous"
        )
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": 0,
            "match_source": "exact",
            "reason": "keyword_score",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": len(selected),
            "invalid_ids_count": 0,
        })
        return SpecificProductResolution(
            status=status,
            products=tuple(selected),
            match_source="exact",
        )

    exact_matches = exact_specific_product_matches(compatible, interpretation)
    if exact_matches and not color_tokens:
        selected = exact_matches[:RERANK_SELECTION_LIMIT]
        status = "exact" if len(selected) == 1 else "ambiguous"
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": 0,
            "match_source": "exact",
        })
        return SpecificProductResolution(
            status=status,
            products=tuple(selected),
            match_source="exact",
        )
    if (
        model_excludes_gmt(interpretation.subject.model)
        and compatible
        and not any(
            product_compatible_with_requested_movement(
                product,
                interpretation.subject.model,
                interpretation.preferences.attributes,
            )
            for product in compatible
        )
    ):
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "reason": "movement_mismatch",
        })
        return SpecificProductResolution(
            status="none",
            products=(),
            match_source="exact",
        )

    settings = get_settings()
    if not compatible:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": 0,
            "invalid_ids_count": 0,
        })
        return SpecificProductResolution(
            status="none",
            products=(),
            match_source="exact",
        )
    if not settings.openai_api_key:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "error_type": "OpenAIUnavailable",
            "reason": "color_mismatch" if color_tokens else "openai_unavailable",
        })
        # Color asked but only other dials in pool — never soft-substitute.
        if color_tokens:
            return SpecificProductResolution(
                status="none",
                products=(),
                match_source="exact",
            )
        raise ProductMatchError("specific_product_match_unavailable")

    # Local keyword miss (common when Vision says "rosa claro" and the catalog
    # title only has "Rosa"): ask GPT to normalize against the full query list.
    candidate_by_id = {
        str(product["id"]): product
        for product in compatible
        if product.get("id") is not None
    }
    gpt_pool = compact_candidates(
        compatible,
        limit=GPT_MATCH_CANDIDATE_LIMIT,
    )
    print("[sales.product.match]", {
        "candidate_count": len(compatible),
        "gpt_pool_count": len(gpt_pool),
        "selected_count": 0,
        "invalid_ids_count": 0,
        "match_source": "openai",
        "reason": "gpt_catalog_normalize",
        "has_color": bool(color_tokens),
    })
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import parse_structured_output

        parse_result = await parse_structured_output(
            model=settings.openai_model,
            text_format=ProductMatchSelection,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Resolva o produto pedido usando SOMENTE itens de CANDIDATES. "
                        "Normalize nomes em inglês/PT (Automatic→Automático, pink→Rosa) e "
                        "ignore descritores de tom (claro, escuro, mostrador). "
                        "Se PREFERENCES.color existir (ex.: rosa), escolha o título que "
                        "contenha essa cor; NÃO substitua por outra cor da mesma linha "
                        "(Kingfisher/Dagger/Azul no lugar de Rosa). "
                        "Use match_status=exact só com um único ID seguro; ambiguous se "
                        "houver 2+ opções da cor/modelo pedidos; none se a cor/modelo "
                        "não estiver na lista. candidate_ids e best_candidate_id devem "
                        "ser IDs de CANDIDATES."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "SUBJECT": interpretation.subject.model_dump(
                                mode="json",
                                exclude_none=True,
                            ),
                            "PREFERENCES": interpretation.preferences.model_dump(
                                mode="json",
                                exclude_none=True,
                            ),
                            "SEARCH_TOKENS": list(
                                catalog_match_tokens(interpretation)
                            ),
                            "CANDIDATES": gpt_pool,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            call_type="product_selection",
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, ProductMatchSelection):
            raise ValueError("product_match_schema_missing")
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        invalid_ids = 0
        for product_id in parsed.candidate_ids[:RERANK_SELECTION_LIMIT]:
            normalized_id = str(product_id)
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            product = candidate_by_id.get(normalized_id)
            if product is None:
                invalid_ids += 1
                continue
            selected.append(product)
        best_candidate_id = (
            str(parsed.best_candidate_id)
            if parsed.best_candidate_id is not None
            else None
        )
        if best_candidate_id is not None and best_candidate_id not in candidate_by_id:
            invalid_ids += 1
            best_candidate_id = None

        status: Literal["exact", "ambiguous", "none"] = "none"
        if parsed.match_status == "exact":
            exact_id = best_candidate_id
            if exact_id is None and len(selected) == 1:
                exact_id = str(selected[0].get("id"))
            exact_product = candidate_by_id.get(exact_id or "")
            if exact_product is not None:
                selected = [exact_product]
                status = "exact"
            else:
                selected = []
        elif parsed.match_status == "ambiguous" and len(selected) >= 2:
            status = "ambiguous"
        else:
            selected = []
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": len(selected),
            "invalid_ids_count": invalid_ids,
            "match_source": "openai",
        })
        print("[sales.product.candidate_selection]", {
            "input_candidate_count": len(compatible),
            "returned_candidate_count": len(selected),
            "invalid_ids_count": invalid_ids,
        })
        return SpecificProductResolution(
            status=status,
            products=tuple(selected),
            match_source="openai",
            invalid_ids_count=invalid_ids,
        )
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError) as exc:
        print("[sales.product.match]", {
            "candidate_count": len(compatible),
            "selected_count": 0,
            "invalid_ids_count": 0,
            "match_source": "exact",
            "error_type": type(exc).__name__,
        })
        raise ProductMatchError("specific_product_match_failed") from exc


def _compact_variants(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    compact: list[dict[str, Any]] = []
    for variant in value[:20]:
        if not isinstance(variant, dict):
            continue
        compact.append({
            key: item
            for key, item in {
                "variant_id": variant.get("variant_id") or variant.get("id"),
                "product_id": variant.get("product_id"),
                "name": variant.get("name"),
                "value": variant.get("value"),
                "color": variant.get("color"),
                "size": variant.get("size"),
                "version": variant.get("version"),
                "reference": variant.get("reference"),
                "sku": variant.get("sku") or variant.get("Sku"),
                "price": variant.get("price"),
                "promotional_price": variant.get("promotional_price"),
                "stock": variant.get("stock"),
                "available": variant.get("available"),
                "available_in_store": variant.get("available_in_store"),
                "availability": variant.get("availability"),
                "VariationSettings": variant.get("VariationSettings"),
            }.items()
            if item is not None
        })
    return compact or None


def _needs_variant_evidence(interpretation: SalesInterpretation) -> bool:
    preferences = interpretation.preferences
    return bool(
        preferences.color
        or preferences.material
        or preferences.attributes
        or "inventory" in interpretation.information_needed
    )


async def enrich_product_variants(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    execute_tool: ToolExecutor,
) -> list[dict[str, Any]]:
    needs_evidence = _needs_variant_evidence(interpretation)
    candidates = _deterministic_semantic_order(products, interpretation)
    candidate_ids = {
        str(product["id"])
        for product in candidates[:MAX_VARIANT_PRODUCT_QUERIES]
        if product.get("id") is not None
    }
    enriched: list[dict[str, Any]] = []
    products_checked = 0
    variants_loaded = 0
    matched_preferences = 0
    preference_terms = [
        _fold(value)
        for value in (
            interpretation.preferences.color,
            interpretation.preferences.material,
            *interpretation.preferences.attributes,
        )
        if value
    ]
    for product in products:
        product_id = str(product.get("id")) if product.get("id") is not None else ""
        should_check = product_id in candidate_ids and (
            needs_evidence or _truth_state(product.get("has_variation")) is True
        )
        if not should_check:
            enriched.append(product)
            continue
        products_checked += 1
        result = await execute_tool(
            "list_product_variants",
            {"product_id": product_id},
        )
        if "error" in result:
            enriched.append(product)
            continue
        variants = result.get("variants") if isinstance(result.get("variants"), list) else []
        variants_loaded += len(variants)
        matched_preferences += sum(
            1
            for variant in variants
            if any(term in _fold(variant) for term in preference_terms)
        )
        enriched.append({**product, "variants": variants})
    print("[sales.variants]", {
        "products_checked": products_checked,
        "variants_loaded": variants_loaded,
        "matched_preferences": matched_preferences,
    })
    return enriched


async def revalidate_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
    execute_tool: ToolExecutor,
) -> tuple[list[dict[str, Any]], bool]:
    refreshed: list[dict[str, Any]] = []
    failed = False
    partial = False
    top_n = revalidate_top_n()
    attempted = 0
    for product in products[:top_n]:
        product_id = product.get("id")
        if product_id is None:
            continue
        attempted += 1
        result = await execute_tool("get_product", {"product_id": str(product_id)})
        if "error" in result:
            failed = True
            partial = True
            continue
        # Revalidation is factual authority: overlay live Tray fields but never
        # invent price/stock when the live payload omits them.
        current = {**product, **result}
        # Drop retrieval-only metadata from customer-facing payload later.
        current["commercial_availability"] = commercial_availability_facts(current)
        current["_revalidated"] = True
        current["_factual_source"] = "tray_live"
        print("[sales.availability.fact]", {
            "has_stock": current["commercial_availability"]["has_stock"],
            "has_lead_time": current["commercial_availability"]["has_lead_time"],
            "immediate_delivery_supported": current["commercial_availability"]["immediate_delivery_supported"],
            "revalidated": True,
        })
        refreshed.append(current)
    if attempted and not refreshed:
        failed = True
        print("[sales.revalidate.total_failure]", {"attempted": attempted})
    elif partial and refreshed:
        print(
            "[sales.revalidate.partial]",
            {
                "attempted": attempted,
                "confirmed": len(refreshed),
                "dropped_stale": attempted - len(refreshed),
            },
        )
    if refreshed:
        refreshed = await enrich_product_variants(refreshed, interpretation, execute_tool)
    # Never present non-revalidated siblings when revalidation partially failed —
    # only confirmed Tray rows may assert live price/stock.
    return refreshed, failed


def _deterministic_semantic_order(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    gender_tokens = preference_gender_tokens(interpretation)
    color_tokens = preference_color_tokens(interpretation)
    terms = [
        _fold(value)
        for value in (
            interpretation.preferences.style,
            interpretation.preferences.color,
            interpretation.preferences.material,
            interpretation.preferences.occasion,
            interpretation.preferences.recipient,
            *interpretation.preferences.attributes,
        )
        if value
    ]
    scored = []
    for index, product in enumerate(products):
        text = _product_text(product)
        base = sum(1 for term in terms if term in text)
        # Strong boost when catalog text evidences requested gender.
        if gender_tokens and product_matches_gender_tokens(product, gender_tokens):
            base += 3
        if color_tokens and product_matches_color_tokens(product, color_tokens):
            base += 4
        scored.append((base, index, product))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [product for _, _, product in scored[:rerank_selection_limit()]]


async def rerank_products(
    products: list[dict[str, Any]],
    interpretation: SalesInterpretation,
) -> list[dict[str, Any]]:
    available_products = [
        product for product in products
        if product_availability_state(product) != "unavailable"
    ]
    settings = get_settings()
    selection_limit = rerank_selection_limit()
    pool_limit = candidate_pool_limit()
    fallback = _deterministic_semantic_order(available_products, interpretation)
    if not available_products or not settings.openai_api_key:
        print("[sales.reranker]", {
            "source": "deterministic_fallback",
            "candidate_count": len(available_products),
            "selected_count": len(fallback),
            "invalid_ids_count": 0,
            "selection_limit": selection_limit,
        })
        return fallback

    # Cap what the LLM may see — never the whole catalog.
    pool = available_products[:pool_limit]
    from .catalog_index import (
        build_allowed_id_sets,
        reject_unknown_rerank_ids,
    )

    candidate_by_id = {
        str(product["id"]): product
        for product in pool
        if product.get("id") is not None
    }
    allowed_sets = build_allowed_id_sets(pool)
    allowed_ids = allowed_sets["allowed_product_ids"]
    prior_order = [str(p["id"]) for p in pool if p.get("id") is not None]
    try:
        from .openai_errors import OpenAIGatewayError
        from .openai_gateway import parse_structured_output

        parse_result = await parse_structured_output(
            model=settings.openai_model,
            text_format=ProductRerankSelection,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classifique produtos reais da NewStore conforme as preferências. "
                        f"Retorne no máximo {selection_limit} IDs presentes em CANDIDATES, "
                        "em ordem de relevância. "
                        "Trate sinônimos de cor (azul=blue, preto=black, branco=white, rosa=pink, "
                        "verde=green, vermelho=red) e gênero (feminino/lady/dama). "
                        "Não invente IDs. Não altere preço, estoque, URL ou disponibilidade. "
                        "Use só evidências dos candidatos (nome, marca, cor, descrição)."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "PREFERENCES": semantic_preferences(interpretation),
                        "COLOR_ALIASES": {
                            token: sorted(expand_color_aliases(token))
                            for token in preference_color_tokens(interpretation)
                        },
                        "ALLOWED_PRODUCT_IDS": sorted(allowed_ids),
                        "ALLOWED_VARIANT_IDS": sorted(
                            allowed_sets["allowed_variant_ids"]
                        ),
                        "CANDIDATES": compact_candidates(pool, limit=pool_limit),
                    }, ensure_ascii=False),
                },
            ],
            call_type="product_selection",
        )
        parsed = parse_result.parsed
        if not isinstance(parsed, ProductRerankSelection):
            raise ValueError("reranker_schema_missing")
        ordered_ids, invalid_ids = reject_unknown_rerank_ids(
            list(parsed.selected_product_ids or []),
            allowed_ids,
            limit=selection_limit,
        )
        selected = [candidate_by_id[pid] for pid in ordered_ids]
        if not selected:
            selected = fallback
        print("[sales.reranker]", {
            "source": "openai",
            "candidate_count": len(pool),
            "selected_count": len(selected),
            "invalid_ids_count": invalid_ids,
            "selection_limit": selection_limit,
            "prior_order_sample": prior_order[:5],
            "posterior_order_sample": ordered_ids[:5],
            "allowed_product_ids": len(allowed_ids),
            "allowed_variant_ids": len(allowed_sets["allowed_variant_ids"]),
        })
        return selected
    except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError) as exc:
        print("[sales.reranker]", {
            "source": "deterministic_fallback",
            "candidate_count": len(available_products),
            "selected_count": len(fallback),
            "invalid_ids_count": 0,
            "error_type": type(exc).__name__,
            "selection_limit": selection_limit,
        })
        return fallback