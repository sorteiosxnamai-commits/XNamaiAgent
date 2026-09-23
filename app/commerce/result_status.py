"""Explicit status for every commerce tool result.

"The provider is down" and "the product does not exist" used to look alike to
callers that only checked for an empty list. The agent must never answer
"nao existe" when what happened was an outage, so every result carries one of:

* ``found``                — the source answered with the requested data;
* ``not_found``            — the source answered: no such product / no match /
                             product no longer sellable;
* ``partial``              — data came back, but some commercial facts (price
                             or stock) are past their validity window;
* ``provider_unavailable`` — no provider, not ready, auth/rate-limit, 5xx;
* ``timeout``              — the provider did not answer in time;
* ``invalid_response``     — the provider answered something unusable;
* ``invalid_request``      — our arguments were rejected (never "not found").
"""

from __future__ import annotations

from enum import Enum
from typing import Any

RESULT_STATUS_KEY = "result_status"


class CommerceResultStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    PARTIAL = "partial"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    INVALID_REQUEST = "invalid_request"

    @property
    def source_answered(self) -> bool:
        """True only when a "not found" can be stated as a fact."""
        return self in {self.FOUND, self.NOT_FOUND, self.PARTIAL}


_NOT_FOUND_ERRORS = frozenset({"product_not_found", "commerce_product_unavailable", "coupon_not_found"})
_INVALID_REQUEST_ERRORS = frozenset({"missing_argument", "unsupported_filter", "unsupported_filters"})
_TIMEOUT_CODES = frozenset({"timeout", "read_timeout", "connect_timeout"})
_INVALID_RESPONSE_CODES = frozenset({"invalid_response"})


def _is_timeout(result: dict[str, Any]) -> bool:
    code = str(result.get("code") or "").casefold()
    error_type = str(result.get("error_type") or "").casefold()
    status = result.get("status_code")
    return code in _TIMEOUT_CODES or "timeout" in error_type or status in (408, 504)


def _facts_expired(products: list[Any]) -> bool:
    for product in products:
        freshness = product.get("freshness") if isinstance(product, dict) else None
        if isinstance(freshness, dict) and (
            freshness.get("price_confirmed") is False or freshness.get("stock_confirmed") is False
        ):
            return True
    return False


def classify_commerce_result(result: Any) -> CommerceResultStatus:
    if not isinstance(result, dict):
        return CommerceResultStatus.INVALID_RESPONSE
    error = result.get("error")
    if error:
        error = str(error)
        if error in _NOT_FOUND_ERRORS:
            return CommerceResultStatus.NOT_FOUND
        if error in _INVALID_REQUEST_ERRORS:
            return CommerceResultStatus.INVALID_REQUEST
        if _is_timeout(result):
            return CommerceResultStatus.TIMEOUT
        if str(result.get("code") or "").casefold() in _INVALID_RESPONSE_CODES:
            return CommerceResultStatus.INVALID_RESPONSE
        # commerce_provider_unavailable, commerce_provider_error, commerce_tool_error, ...
        return CommerceResultStatus.PROVIDER_UNAVAILABLE
    if result.get("ok") is False:
        return CommerceResultStatus.INVALID_RESPONSE

    if "products" in result:
        products = result.get("products")
        if not isinstance(products, list):
            return CommerceResultStatus.INVALID_RESPONSE
        if not products:
            return CommerceResultStatus.NOT_FOUND
        return CommerceResultStatus.PARTIAL if _facts_expired(products) else CommerceResultStatus.FOUND
    product = result.get("product")
    if isinstance(product, dict):
        return CommerceResultStatus.PARTIAL if _facts_expired([product]) else CommerceResultStatus.FOUND
    if result.get("stock_confirmed") is False or result.get("price_confirmed") is False:
        return CommerceResultStatus.PARTIAL
    return CommerceResultStatus.FOUND


def with_result_status(result: Any) -> Any:
    """Annotate a tool result in place (dicts only) and return it."""
    if isinstance(result, dict):
        result[RESULT_STATUS_KEY] = classify_commerce_result(result).value
    return result


def result_status(result: Any) -> CommerceResultStatus:
    """Status already stamped by ``execute_tool``, or classified now."""
    if isinstance(result, dict):
        raw = result.get(RESULT_STATUS_KEY)
        if isinstance(raw, str):
            try:
                return CommerceResultStatus(raw)
            except ValueError:
                pass
    return classify_commerce_result(result)


__all__ = [
    "CommerceResultStatus",
    "RESULT_STATUS_KEY",
    "classify_commerce_result",
    "result_status",
    "with_result_status",
]
