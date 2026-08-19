"""Typed OpenAI gateway errors (Responses + Chat Completions)."""

from __future__ import annotations


class OpenAIGatewayError(RuntimeError):
    """Base error for the OpenAI gateway layer."""

    code: str = "openai_gateway_error"

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(message or self.code)


class OpenAIRefusalError(OpenAIGatewayError):
    code = "openai_refusal"


class OpenAIIncompleteError(OpenAIGatewayError):
    code = "openai_incomplete"


class OpenAIEmptyOutputError(OpenAIGatewayError):
    code = "openai_empty_output"


class OpenAISchemaError(OpenAIGatewayError):
    code = "openai_schema_invalid"


class OpenAITimeoutGatewayError(OpenAIGatewayError):
    code = "openai_timeout"


class OpenAIRateLimitGatewayError(OpenAIGatewayError):
    code = "openai_rate_limit"


class OpenAIUnknownToolError(OpenAIGatewayError):
    code = "openai_unknown_tool"


class OpenAIInvalidToolArgumentsError(OpenAIGatewayError):
    code = "openai_invalid_tool_arguments"


class OpenAIToolLoopDisabledError(OpenAIGatewayError):
    code = "openai_tool_loop_disabled"
