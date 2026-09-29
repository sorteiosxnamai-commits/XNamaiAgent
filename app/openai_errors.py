"""Typed OpenAI gateway errors (Responses + Chat Completions).

Every error carries a ``category`` so logs/metrics can tell a quota problem from
a slow provider. The customer never sees any of this: callers answer with a
neutral operational message regardless of category.
"""

from __future__ import annotations

import asyncio
from typing import Literal

LLMErrorCategory = Literal[
    "rate_limited",  # 429 transient: retry later is fine
    "quota_exhausted",  # 429 insufficient_quota / billing: retrying will not help
    "timeout",
    "provider_unavailable",  # connection failure, 5xx
    "invalid_request",  # 400/404/409/422: our request is wrong
    "auth_error",  # 401/403: key/permission/config
    "invalid_response",  # refusal, incomplete, empty, schema/tool mismatch
    "internal_error",  # anything we could not place (our own bug included)
]


class OpenAIGatewayError(RuntimeError):
    """Base error for the OpenAI gateway layer."""

    code: str = "openai_gateway_error"
    category: LLMErrorCategory = "internal_error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        category: LLMErrorCategory | None = None,
    ) -> None:
        self.code = code or self.code
        self.category = category or self.category
        super().__init__(message or self.code)


class OpenAIRefusalError(OpenAIGatewayError):
    code = "openai_refusal"
    category = "invalid_response"


class OpenAIIncompleteError(OpenAIGatewayError):
    code = "openai_incomplete"
    category = "invalid_response"


class OpenAIEmptyOutputError(OpenAIGatewayError):
    code = "openai_empty_output"
    category = "invalid_response"


class OpenAISchemaError(OpenAIGatewayError):
    code = "openai_schema_invalid"
    category = "invalid_response"


class OpenAITimeoutGatewayError(OpenAIGatewayError):
    code = "openai_timeout"
    category = "timeout"


class OpenAIRateLimitGatewayError(OpenAIGatewayError):
    code = "openai_rate_limit"
    category = "rate_limited"


class OpenAIQuotaExhaustedError(OpenAIGatewayError):
    code = "openai_quota_exhausted"
    category = "quota_exhausted"


class OpenAIProviderUnavailableError(OpenAIGatewayError):
    code = "openai_provider_unavailable"
    category = "provider_unavailable"


class OpenAIInvalidRequestError(OpenAIGatewayError):
    code = "openai_invalid_request"
    category = "invalid_request"


class OpenAIAuthError(OpenAIGatewayError):
    code = "openai_auth_error"
    category = "auth_error"


class OpenAIUnknownToolError(OpenAIGatewayError):
    code = "openai_unknown_tool"
    category = "invalid_response"


class OpenAIInvalidToolArgumentsError(OpenAIGatewayError):
    code = "openai_invalid_tool_arguments"
    category = "invalid_response"


class OpenAIToolLoopDisabledError(OpenAIGatewayError):
    code = "openai_tool_loop_disabled"
    category = "internal_error"


#: OpenAI 429 bodies that mean billing/quota, not a transient burst.
_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"})


def _provider_error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code.casefold()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nested = body.get("error") if isinstance(body.get("error"), dict) else body
        value = nested.get("code") or nested.get("type")
        if isinstance(value, str):
            return value.casefold()
    return ""


def classify_llm_error(exc: BaseException) -> LLMErrorCategory:
    """Map any SDK/gateway/runtime exception to the taxonomy above."""
    if isinstance(exc, OpenAIGatewayError):
        return exc.category
    import openai

    if isinstance(exc, (openai.APITimeoutError, asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, openai.RateLimitError):
        return "quota_exhausted" if _provider_error_code(exc) in _QUOTA_CODES else "rate_limited"
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "auth_error"
    if isinstance(
        exc,
        (openai.BadRequestError, openai.NotFoundError, openai.ConflictError, openai.UnprocessableEntityError),
    ):
        return "invalid_request"
    if isinstance(exc, (openai.APIConnectionError, openai.InternalServerError)):
        return "provider_unavailable"
    if isinstance(exc, openai.APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0)
        if status >= 500:
            return "provider_unavailable"
        if 400 <= status < 500:
            return "invalid_request"
    return "internal_error"


_CATEGORY_ERRORS: dict[str, type[OpenAIGatewayError]] = {
    "timeout": OpenAITimeoutGatewayError,
    "rate_limited": OpenAIRateLimitGatewayError,
    "quota_exhausted": OpenAIQuotaExhaustedError,
    "provider_unavailable": OpenAIProviderUnavailableError,
    "invalid_request": OpenAIInvalidRequestError,
    "auth_error": OpenAIAuthError,
}


def gateway_error_for(exc: BaseException) -> OpenAIGatewayError:
    """Typed gateway error for ``exc``. Messages are truncated, never secrets."""
    if isinstance(exc, OpenAIGatewayError):
        return exc
    category = classify_llm_error(exc)
    error_class = _CATEGORY_ERRORS.get(category)
    if error_class is not None:
        return error_class(str(exc)[:240])
    return OpenAIGatewayError(str(exc)[:240], code="openai_api_error", category=category)
