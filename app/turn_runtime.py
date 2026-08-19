from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr


LLMCallType = Literal[
    "decision",
    "clarification",
    "product_selection",
    "response_composition",
    "checkout_repair",
    "judge",
    "audio_transcription",
    "audio_tts",
    "legacy",
]
ExecutionPath = Literal["fast", "normal", "complex", "critical"]


class LLMCallBudgetExceeded(RuntimeError):
    pass


class LLMCallBudget(BaseModel):
    max_calls: int = Field(default=3, ge=0)
    used_calls: int = Field(default=0, ge=0)
    enforce: bool = False
    allowed_call_types: set[str] = Field(default_factory=set)

    def reserve(self, call_type: str) -> None:
        blocked_type = bool(
            self.allowed_call_types
            and call_type not in self.allowed_call_types
        )
        exhausted = self.used_calls >= self.max_calls
        if self.enforce and (blocked_type or exhausted):
            raise LLMCallBudgetExceeded(
                f"llm_call_budget_exceeded:{call_type}"
            )
        self.used_calls += 1


class TurnRuntimeContext(BaseModel):
    trace_id: str
    inbound_id: int | None = None
    conversation_key: str = "unresolved"
    channel: str = "unknown"
    started_at: float = Field(default_factory=time.perf_counter)

    openai_call_count: int = 0
    # Logical ops vs transport attempts (Responses→Chat fallback = 1 logical, 2 transport).
    logical_llm_calls: int = 0
    openai_transport_attempts: int = 0
    responses_attempts: int = 0
    chat_fallback_attempts: int = 0
    tray_call_count: int = 0
    database_call_count: int = 0
    openai_input_tokens: int = 0
    openai_output_tokens: int = 0
    openai_cached_tokens: int = 0
    openai_reasoning_tokens: int = 0
    openai_api_route: str | None = None
    openai_api_fallback: bool = False

    execution_path: ExecutionPath = "fast"
    judge_triggered: bool = False
    judge_mode: str = "disabled"
    risk_score: int = Field(default=0, ge=0, le=100)
    fallback_reasons: list[str] = Field(default_factory=list)
    stage_durations_ms: dict[str, float] = Field(default_factory=dict)
    llm_calls_by_type: dict[str, int] = Field(default_factory=dict)
    llm_call_reasons: list[dict[str, object]] = Field(default_factory=list)
    llm_calls_avoided: int = 0
    llm_avoided_reasons: list[dict[str, object]] = Field(default_factory=list)
    integration_failures: dict[str, int] = Field(default_factory=dict)
    llm_budget: LLMCallBudget = Field(default_factory=LLMCallBudget)
    tray_calls: list[dict[str, object]] = Field(default_factory=list)
    openai_calls: list[dict[str, object]] = Field(default_factory=list)
    context_snapshot: dict[str, object] = Field(default_factory=dict)
    inbound_snapshot: dict[str, object] = Field(default_factory=dict)
    outbound_snapshot: dict[str, object] = Field(default_factory=dict)

    _stage_started_at: dict[str, float] = PrivateAttr(default_factory=dict)

    def start_stage(self, name: str) -> None:
        self._stage_started_at[name] = time.perf_counter()

    def finish_stage(self, name: str) -> None:
        started_at = self._stage_started_at.pop(name, None)
        if started_at is None:
            return
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.stage_durations_ms[name] = round(
            self.stage_durations_ms.get(name, 0.0) + elapsed_ms,
            2,
        )

    def register_avoided_llm_call(
        self,
        reason: str,
        *,
        intended_call_type: str | None = None,
        intended_call_types: list[str] | None = None,
    ) -> None:
        types = list(intended_call_types or [])
        if intended_call_type:
            types.append(intended_call_type)
        if not types:
            types = ["unspecified"]
        for call_type in types:
            self.llm_calls_avoided += 1
            self.llm_avoided_reasons.append(
                {
                    "reason": reason,
                    "intended_call_type": call_type,
                }
            )

    def register_transport_attempt(
        self,
        transport: Literal["responses", "chat_completions"] | str,
    ) -> None:
        """Never refunded — Observability of every OpenAI HTTP attempt."""
        self.openai_transport_attempts += 1
        name = str(transport or "").strip().casefold()
        if name.startswith("response"):
            self.responses_attempts += 1
        elif "chat" in name:
            self.chat_fallback_attempts += 1

    def register_openai_call(
        self,
        call_type: str,
        *,
        reason: str | None = None,
        transport: str | None = None,
    ) -> None:
        try:
            self.llm_budget.reserve(call_type)
        except LLMCallBudgetExceeded:
            self.register_avoided_llm_call(
                reason or "budget_exceeded",
                intended_call_type=call_type,
            )
            self.register_fallback("llm_budget_exceeded")
            raise
        self.openai_call_count += 1
        self.logical_llm_calls = self.openai_call_count
        if transport:
            self.register_transport_attempt(transport)
        else:
            # Default: count as one transport when gateway did not annotate.
            self.register_transport_attempt(
                "chat_completions"
                if "fallback_chat" in str(call_type)
                else (self.openai_api_route or "responses")
            )
        self.llm_calls_by_type[call_type] = (
            self.llm_calls_by_type.get(call_type, 0) + 1
        )
        self.llm_call_reasons.append(
            {
                "call_type": call_type,
                "reason": reason or call_type,
                "index": self.openai_call_count,
                "logical": True,
            }
        )
        if self.openai_call_count == 1 and self.execution_path == "fast":
            self.execution_path = "normal"
        elif self.openai_call_count >= 2 and self.execution_path != "critical":
            self.execution_path = "complex"

    def promote_budget(self, new_max_calls: int) -> None:
        """Raise the ceiling without resetting consumed logical calls."""
        try:
            ceiling = max(0, int(new_max_calls))
        except (TypeError, ValueError):
            return
        self.llm_budget.max_calls = max(self.llm_budget.max_calls, ceiling)
        if self.execution_path not in {"critical"}:
            self.execution_path = "complex"

    def release_failed_openai_attempt(self, call_type: str) -> None:
        """Refund logical budget only when Responses fails and Chat will retry.

        Transport attempt counters are never refunded (Etapa 7).
        """
        if self.llm_budget.used_calls > 0:
            self.llm_budget.used_calls -= 1
        if self.openai_call_count > 0:
            self.openai_call_count -= 1
        self.logical_llm_calls = self.openai_call_count
        count = self.llm_calls_by_type.get(call_type, 0)
        if count <= 1:
            self.llm_calls_by_type.pop(call_type, None)
        else:
            self.llm_calls_by_type[call_type] = count - 1
        if self.llm_call_reasons and self.llm_call_reasons[-1].get("call_type") == call_type:
            self.llm_call_reasons.pop()
        # Keep openai_calls / transport_* for observability of the failed attempt.

    def register_openai_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> None:
        self.openai_input_tokens += max(0, int(input_tokens or 0))
        self.openai_output_tokens += max(0, int(output_tokens or 0))
        self.openai_cached_tokens += max(0, int(cached_tokens or 0))
        self.openai_reasoning_tokens += max(0, int(reasoning_tokens or 0))

    def register_integration_failure(self, provider: str) -> None:
        self.integration_failures[provider] = (
            self.integration_failures.get(provider, 0) + 1
        )

    def register_fallback(self, reason: str | None) -> None:
        if reason and reason not in self.fallback_reasons:
            self.fallback_reasons.append(reason)

    def mark_critical(self, risk_score: int | None = None) -> None:
        self.execution_path = "critical"
        if risk_score is not None:
            self.risk_score = max(0, min(100, int(risk_score)))

    def safe_summary(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "inbound_id": self.inbound_id,
            "channel": self.channel,
            "conversation_key_present": self.conversation_key != "unresolved",
            "execution_path": self.execution_path,
            "openai_call_count": self.openai_call_count,
            "logical_llm_calls": self.logical_llm_calls,
            "openai_transport_attempts": self.openai_transport_attempts,
            "responses_attempts": self.responses_attempts,
            "chat_fallback_attempts": self.chat_fallback_attempts,
            "tray_call_count": self.tray_call_count,
            "database_call_count": self.database_call_count,
            "openai_input_tokens": self.openai_input_tokens,
            "openai_output_tokens": self.openai_output_tokens,
            "openai_cached_tokens": self.openai_cached_tokens,
            "openai_reasoning_tokens": self.openai_reasoning_tokens,
            "judge_triggered": self.judge_triggered,
            "judge_mode": self.judge_mode,
            "risk_score": self.risk_score,
            "fallback_reasons": list(self.fallback_reasons),
            "stage_durations_ms": dict(self.stage_durations_ms),
            "llm_calls_by_type": dict(self.llm_calls_by_type),
            "llm_call_reasons": list(self.llm_call_reasons[:12]),
            "llm_calls_avoided": self.llm_calls_avoided,
            "llm_avoided_reasons": list(self.llm_avoided_reasons[:12]),
            "llm_budget": {
                "max_calls": self.llm_budget.max_calls,
                "used_calls": self.llm_budget.used_calls,
                "enforce": self.llm_budget.enforce,
            },
            "integration_failures": dict(self.integration_failures),
            "tray_tools": [
                {
                    "tool": item.get("tool"),
                    "ok": item.get("ok"),
                    "elapsed_ms": item.get("elapsed_ms"),
                }
                for item in self.tray_calls[:20]
            ],
            "openai_calls": list(self.openai_calls[:12]),
            "inbound": dict(self.inbound_snapshot),
            "context": dict(self.context_snapshot),
            "outbound": dict(self.outbound_snapshot),
            "processing_total_ms": round(
                (time.perf_counter() - self.started_at) * 1000,
                2,
            ),
        }
