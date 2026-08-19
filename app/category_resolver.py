from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel

from .config import get_settings
from .openai_runtime import execute_openai_call
from .turn_runtime import LLMCallBudgetExceeded


CATEGORY_PAGE_LIMIT = 50
MAX_CATEGORY_PAGES = 5
MAX_SELECTED_CATEGORIES = 2
MAX_CATEGORY_PRODUCT_QUERIES = 5

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class CategorySelection(BaseModel):
    selected_category_ids: list[str]


@dataclass(frozen=True)
class CategoryResolution:
    selected_category_ids: tuple[str, ...] = ()
    descendant_category_ids: tuple[str, ...] = ()
    source: str = "not_found"
    categories_loaded: int = 0
    lookup_failed: bool = False
    failure_reason: str | None = None

    @property
    def product_category_ids(self) -> tuple[str, ...]:
        ordered = (*self.selected_category_ids, *self.descendant_category_ids)
        return tuple(dict.fromkeys(ordered))[:MAX_CATEGORY_PRODUCT_QUERIES]


def _fold(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "").lower())
        if not unicodedata.combining(char)
    )
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _singular_token(token: str) -> str:
    if len(token) > 4 and token.endswith("oes"):
        return token[:-3] + "ao"
    if len(token) > 4 and token.endswith("ais"):
        return token[:-3] + "al"
    if len(token) > 4 and token.endswith("eis"):
        return token[:-3] + "el"
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def normalize_category_name(value: Any) -> str:
    return " ".join(_singular_token(token) for token in _fold(value).split())


def _category_id(category: dict[str, Any]) -> str | None:
    value = category.get("id")
    return str(value) if value is not None else None


def _match_score(product_type: str, category: dict[str, Any]) -> float:
    expected = normalize_category_name(product_type)
    candidate = normalize_category_name(category.get("name"))
    if not expected or not candidate:
        return 0.0
    if expected == candidate:
        return 1.0
    expected_tokens = set(expected.split())
    candidate_tokens = set(candidate.split())
    if expected in candidate or candidate in expected:
        return 0.85
    overlap = len(expected_tokens & candidate_tokens)
    return overlap / max(len(expected_tokens), 1) * 0.75


def _flatten_tree(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_flatten_tree(item))
        return found
    if not isinstance(value, dict):
        return found
    if value.get("id") is not None and value.get("name") is not None:
        found.append(value)
    for key in ("children", "subcategories", "categories", "items", "data", "tree", "category"):
        if key in value:
            found.extend(_flatten_tree(value[key]))
    return found


class CategoryResolver:
    def __init__(self, execute_tool: ToolExecutor):
        self._execute_tool = execute_tool

    @staticmethod
    def _unambiguous_match(
        product_type: str,
        categories: list[dict[str, Any]],
    ) -> bool:
        expected = normalize_category_name(product_type)
        matches = [
            category
            for category in categories
            if normalize_category_name(category.get("name")) == expected
        ]
        return len(matches) == 1

    @staticmethod
    def _as_non_negative_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    async def _load_categories(
        self,
        product_type: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        categories: list[dict[str, Any]] = []
        seen: set[str] = set()
        records_loaded = 0
        for page in range(1, MAX_CATEGORY_PAGES + 1):
            print("[sales.category.request]", {
                "page": page,
                "limit": CATEGORY_PAGE_LIMIT,
            })
            result = await self._execute_tool(
                "list_categories",
                {"limit": CATEGORY_PAGE_LIMIT, "page": page},
            )
            if "error" in result:
                return categories, str(
                    result.get("error_reason") or "category_adapter_error"
                )
            page_categories = result.get("categories")
            if not isinstance(page_categories, list):
                page_categories = []
            returned_count = len(page_categories)
            records_loaded += returned_count
            for category in page_categories:
                if not isinstance(category, dict):
                    continue
                category_id = _category_id(category)
                if not category_id or category_id in seen:
                    continue
                seen.add(category_id)
                categories.append(category)

            paging = result.get("paging")
            paging = paging if isinstance(paging, dict) else {}
            total = self._as_non_negative_int(paging.get("total"))
            response_page = self._as_non_negative_int(paging.get("page"))
            response_limit = self._as_non_negative_int(paging.get("limit"))
            if total is not None:
                has_more = records_loaded < total
            else:
                has_more = returned_count >= (
                    response_limit or CATEGORY_PAGE_LIMIT
                )
            print("[sales.category.page]", {
                "page": response_page or page,
                "returned_count": returned_count,
                "total": total,
                "has_more": has_more,
            })

            if returned_count == 0:
                break
            if self._unambiguous_match(product_type, categories):
                break
            if not has_more:
                break
        return categories, None

    async def _select_ambiguous(
        self,
        product_type: str,
        candidates: list[dict[str, Any]],
    ) -> tuple[list[str], str]:
        valid_id_list = [
            category_id
            for category in candidates
            if (category_id := _category_id(category)) is not None
        ]
        valid_ids = set(valid_id_list)
        settings = get_settings()
        if not settings.openai_api_key:
            return valid_id_list[:MAX_SELECTED_CATEGORIES], "normalized"
        compact = [
            {
                "id": _category_id(category),
                "name": category.get("name"),
                "parent_id": category.get("parent_id"),
            }
            for category in candidates[:20]
        ]
        try:
            from .openai_errors import OpenAIGatewayError
            from .openai_gateway import parse_structured_output

            parse_result = await parse_structured_output(
                model=settings.openai_model,
                text_format=CategorySelection,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Selecione no máximo duas categorias reais compatíveis com o tipo de produto. "
                            "Retorne somente IDs presentes em CATEGORIES e não invente IDs."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"product_type": product_type, "CATEGORIES": compact},
                            ensure_ascii=False,
                        ),
                    },
                ],
                call_type="product_selection",
            )
            parsed = parse_result.parsed
            if not isinstance(parsed, CategorySelection):
                raise ValueError("category_selector_schema_missing")
            selected = [
                str(category_id)
                for category_id in parsed.selected_category_ids
                if str(category_id) in valid_ids
            ]
            return list(dict.fromkeys(selected))[:MAX_SELECTED_CATEGORIES], "openai"
        except (APIError, OpenAIGatewayError, LLMCallBudgetExceeded, ValueError, TypeError):
            return valid_id_list[:MAX_SELECTED_CATEGORIES], "normalized"

    async def _load_descendants(self, selected_ids: list[str]) -> list[str]:
        descendants: list[str] = []
        selected_set = set(selected_ids)
        for category_id in selected_ids:
            if len(selected_ids) + len(descendants) >= MAX_CATEGORY_PRODUCT_QUERIES:
                break
            result = await self._execute_tool(
                "get_category_tree",
                {"category_id": category_id},
            )
            if "error" in result:
                continue
            for category in _flatten_tree(result.get("tree")):
                descendant_id = _category_id(category)
                if not descendant_id or descendant_id in selected_set or descendant_id in descendants:
                    continue
                descendants.append(descendant_id)
                if len(selected_ids) + len(descendants) >= MAX_CATEGORY_PRODUCT_QUERIES:
                    break
        return descendants

    async def resolve(self, product_type: str | None) -> CategoryResolution:
        if not product_type or not product_type.strip():
            resolution = CategoryResolution()
            self._log(resolution)
            return resolution

        categories, failure_reason = await self._load_categories(product_type)
        if failure_reason:
            resolution = CategoryResolution(
                categories_loaded=len(categories),
                lookup_failed=True,
                failure_reason=failure_reason,
            )
            self._log(resolution)
            return resolution

        folded_expected = _fold(product_type)
        exact = [item for item in categories if _fold(item.get("name")) == folded_expected]
        source = "exact"
        candidates = exact
        if not candidates:
            normalized_expected = normalize_category_name(product_type)
            candidates = [
                item
                for item in categories
                if normalize_category_name(item.get("name")) == normalized_expected
            ]
            source = "normalized"
        if not candidates:
            scored = sorted(
                (
                    (_match_score(product_type, item), item)
                    for item in categories
                ),
                key=lambda pair: pair[0],
                reverse=True,
            )
            candidates = [item for score, item in scored if score >= 0.5 and score == scored[0][0]] if scored else []
            source = "normalized"

        if not candidates:
            resolution = CategoryResolution(
                categories_loaded=len(categories),
                failure_reason=(
                    "category_empty" if not categories else "category_not_found"
                ),
            )
            self._log(resolution)
            return resolution

        if len(candidates) > 1:
            selected_ids, source = await self._select_ambiguous(product_type, candidates)
        else:
            selected_id = _category_id(candidates[0])
            selected_ids = [selected_id] if selected_id else []
        descendants = await self._load_descendants(selected_ids)
        resolution = CategoryResolution(
            selected_category_ids=tuple(selected_ids),
            descendant_category_ids=tuple(descendants),
            source=source,
            categories_loaded=len(categories),
        )
        self._log(resolution)
        return resolution

    @staticmethod
    def _log(resolution: CategoryResolution) -> None:
        print("[sales.category.resolve]", {
            "source": resolution.source,
            "categories_loaded": resolution.categories_loaded,
            "selected_count": len(resolution.selected_category_ids),
            "descendant_count": len(resolution.descendant_category_ids),
            "reason": resolution.failure_reason,
        })
