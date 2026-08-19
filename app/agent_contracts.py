from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import AgentResult, IncomingMessage


AgentDomain = Literal[
    "commerce",
    "raffle",
    "store_general",
    "greeting",
    "guardrail",
    "out_of_scope",
    "unknown",
]
DecisionSource = Literal[
    "deterministic",
    "openai",
    "tool",
    "fallback",
    "guardrail",
    "unknown",
]
ExecutionPath = Literal["fast", "normal", "complex", "critical"]


class ToolCallContract(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    mutating: bool = False


class ToolResultContract(BaseModel):
    name: str
    ok: bool
    source: str = "unknown"
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    elapsed_ms: float | None = Field(default=None, ge=0)


class RiskAssessment(BaseModel):
    score: int = Field(default=0, ge=0, le=100)
    level: Literal["low", "medium", "high", "critical"] = "low"
    reasons: list[str] = Field(default_factory=list)
    required_validations: list[str] = Field(default_factory=list)


class AgentDecision(BaseModel):
    domain: AgentDomain = "unknown"
    intent: str = "general_support"
    goal: str | None = None
    action: str | None = None
    source: DecisionSource = "unknown"
    confidence: float | None = Field(default=None, ge=0, le=1)
    needs_clarification: bool = False
    handoff_required: bool = False
    fast_path: bool = False
    execution_path: ExecutionPath = "fast"
    tool_calls: list[ToolCallContract] = Field(default_factory=list)
    risk: RiskAssessment = Field(default_factory=RiskAssessment)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionSnapshot(BaseModel):
    decision: AgentDecision
    factual_validation_required: bool = False
    policy_mode: Literal["off", "shadow", "enforce"] = "shadow"
    policy_action: Literal["allow", "review", "block"] = "allow"
    policy_reasons: list[str] = Field(default_factory=list)


_URL_RE = re.compile(r"https?://[^\s<>()]+", flags=re.IGNORECASE)


def _decision_source(result: AgentResult) -> DecisionSource:
    source = str((result.response_metadata or {}).get("response_source") or "")
    if source in {"openai", "tool", "guardrail"}:
        return source
    if source in {
        "deterministic",
        "deterministic_fallback",
        "local",
        "template",
    }:
        return "fallback" if "fallback" in source else "deterministic"
    if result.safety_reason and result.handoff_required:
        return "guardrail"
    return "unknown"


def _risk_assessment(result: AgentResult) -> RiskAssessment:
    metadata = result.response_metadata or {}
    commercial_data = result.commercial_data or {}
    text = result.reply_text or ""
    reasons: list[str] = []
    validations: list[str] = []
    score = 10

    if result.handoff_required:
        score = max(score, 90)
        reasons.append("human_handoff")
    if result.intent in {"payment", "order_payment", "checkout", "order"}:
        score = max(score, 80)
        reasons.append("transactional_intent")
    if any(
        key in commercial_data
        for key in (
            "order",
            "order_id",
            "payment",
            "payment_url",
            "cart_url",
            "shipping",
        )
    ):
        score = max(score, 75)
        reasons.append("transactional_facts")
    if metadata.get("used_tray"):
        score = max(score, 40)
        reasons.append("external_commerce_facts")
    if _URL_RE.search(text):
        validations.append("urls")
        score = max(score, 45)
    if any(
        reason in reasons
        for reason in ("transactional_intent", "transactional_facts")
    ):
        validations.append("transactional_facts")
    if any(key in commercial_data for key in ("price", "products", "inventory")):
        validations.append("catalog_facts")

    level: Literal["low", "medium", "high", "critical"]
    if score >= 90:
        level = "critical"
    elif score >= 70:
        level = "high"
    elif score >= 40:
        level = "medium"
    else:
        level = "low"
    return RiskAssessment(
        score=score,
        level=level,
        reasons=list(dict.fromkeys(reasons)),
        required_validations=list(dict.fromkeys(validations)),
    )


def build_agent_decision(
    incoming: IncomingMessage,
    result: AgentResult,
    *,
    openai_call_count: int = 0,
) -> AgentDecision:
    metadata = result.response_metadata or {}
    domain = str(metadata.get("domain") or "unknown")
    allowed_domains = {
        "commerce",
        "raffle",
        "store_general",
        "greeting",
        "guardrail",
        "out_of_scope",
        "unknown",
    }
    if domain not in allowed_domains:
        domain = "unknown"

    risk = _risk_assessment(result)
    if risk.level in {"high", "critical"}:
        execution_path: ExecutionPath = "critical"
    elif openai_call_count >= 2:
        execution_path = "complex"
    elif openai_call_count == 1:
        execution_path = "normal"
    else:
        execution_path = "fast"

    commercial_data = result.commercial_data or {}
    action = (
        metadata.get("pending_action")
        or commercial_data.get("action")
        or commercial_data.get("stage")
    )
    return AgentDecision(
        domain=domain,  # type: ignore[arg-type]
        intent=result.intent,
        goal=metadata.get("goal"),
        action=str(action) if action else None,
        source=_decision_source(result),
        confidence=result.confidence,
        needs_clarification=result.safety_reason in {
            "commerce_clarification",
            "checkout_data_incomplete",
        },
        handoff_required=result.handoff_required,
        fast_path=execution_path == "fast",
        execution_path=execution_path,
        risk=risk,
        metadata={
            "channel": incoming.channel,
            "response_source": metadata.get("response_source"),
            "used_tray": bool(metadata.get("used_tray")),
            "safety_reason": result.safety_reason,
        },
    )


def evaluate_policy(
    decision: AgentDecision,
    *,
    mode: Literal["off", "shadow", "enforce"] = "shadow",
) -> DecisionSnapshot:
    reasons: list[str] = []
    action: Literal["allow", "review", "block"] = "allow"
    if decision.risk.level == "critical":
        action = "review"
        reasons.append("critical_risk")
    if (
        decision.risk.required_validations
        and decision.source in {"openai", "unknown"}
    ):
        action = "review"
        reasons.append("facts_require_validation")
    if decision.handoff_required:
        action = "review"
        reasons.append("handoff_required")

    return DecisionSnapshot(
        decision=decision,
        factual_validation_required=bool(
            decision.risk.required_validations
        ),
        policy_mode=mode,
        policy_action=action,
        policy_reasons=list(dict.fromkeys(reasons)),
    )
