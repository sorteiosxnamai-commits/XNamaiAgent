from __future__ import annotations

from typing import Any


def score_eval_case(
    case: dict[str, Any],
    *,
    observed_domain: str | None,
    observed_tools: list[str] | None = None,
    reply_text: str | None = None,
    openai_calls: int = 0,
    factual_valid: bool = True,
    handoff_required: bool = False,
    invented_claim: bool = False,
) -> dict[str, Any]:
    expected = case.get("expected") or {}
    tools = list(observed_tools or [])
    reply = reply_text or ""
    checks: dict[str, bool] = {}

    checks["domain"] = (
        expected.get("domain") is None
        or observed_domain == expected.get("domain")
    )
    must_call = list(expected.get("must_call_tools") or [])
    checks["tools_required"] = all(tool in tools for tool in must_call)
    must_not = list(expected.get("must_not_call_tools") or [])
    checks["tools_forbidden"] = not any(tool in tools for tool in must_not)
    checks["openai_budget"] = openai_calls <= int(expected.get("max_openai_calls") or 99)
    checks["no_invention"] = not invented_claim
    if expected.get("requires_factual_support"):
        checks["factual_support"] = bool(factual_valid)
    else:
        checks["factual_support"] = True
    checks["handoff"] = bool(handoff_required) == bool(expected.get("handoff_required"))
    for needle in expected.get("must_include") or []:
        checks[f"include:{needle}"] = needle.casefold() in reply.casefold()
    for needle in expected.get("must_not_include") or []:
        checks[f"exclude:{needle}"] = needle.casefold() not in reply.casefold()

    passed = all(checks.values())
    score = round(100.0 * (sum(1 for ok in checks.values() if ok) / max(len(checks), 1)), 2)
    return {
        "id": case.get("id"),
        "passed": passed,
        "score": score,
        "checks": checks,
    }
