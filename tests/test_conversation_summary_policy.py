from app.conversation_summary_policy import (
    evaluate_summary_delta,
    format_conversation_summary_block,
    sanitize_summary_delta,
    should_apply_summary_delta,
)
from app.memory_models import ConversationSummaryDelta


def test_empty_or_noise_summary_is_skipped():
    assert should_apply_summary_delta(None) is False
    assert (
        should_apply_summary_delta(
            ConversationSummaryDelta(open_questions=["só uma?"])
        )
        is False
    )
    assert (
        should_apply_summary_delta(
            ConversationSummaryDelta(current_goal="comprar"),
            existing={"current_goal": "comprar"},
        )
        is False
    )


def test_meaningful_summary_deltas_are_applied():
    assert should_apply_summary_delta(
        ConversationSummaryDelta(commitments=["cliente pediu link"])
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(user_corrections=["não é GMT"])
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(resolved_points=["marca confirmada"])
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(last_failure="tray_timeout")
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(current_goal="pagar pedido"),
        existing={"current_goal": "buscar produto"},
    )
    assert should_apply_summary_delta(
        ConversationSummaryDelta(open_questions=["cor?", "orçamento?"])
    )


def test_sanitize_rejects_commercial_price_and_stock():
    cleaned, codes = sanitize_summary_delta(
        ConversationSummaryDelta(
            commitments=["Seastar custa R$ 1990 e tem estoque"],
            current_goal="fechar",
        )
    )
    assert cleaned is not None
    assert cleaned.commitments == []
    assert cleaned.current_goal == "fechar"
    assert "commercial_volatile" in codes


def test_sanitize_rejects_sensitive_and_url_only_delta():
    cleaned, codes = sanitize_summary_delta(
        ConversationSummaryDelta(
            commitments=["CVV 123", "https://evil.example/pay"],
        )
    )
    assert cleaned is None
    assert "sensitive" in codes or "url_blocked" in codes
    assert "empty_after_sanitize" in codes


def test_evaluate_keeps_safe_commitment():
    ok, cleaned, codes = evaluate_summary_delta(
        ConversationSummaryDelta(
            commitments=["cliente prefere Tissot"],
            open_questions=["orçamento?", "cor?"],
        )
    )
    assert ok is True
    assert cleaned is not None
    assert cleaned.commitments == ["cliente prefere Tissot"]
    assert codes == []


def test_format_summary_block_is_non_authoritative():
    block = format_conversation_summary_block(
        {
            "current_goal": "buscar Tissot",
            "summary": "goal=buscar Tissot",
            "open_questions": ["cor?"],
            "resolved_points": [],
            "user_corrections": [],
            "commitments": [],
        }
    )
    assert "<conversation_summary>" in block
    assert "NÃO use como fonte de preço" in block
    assert "buscar Tissot" in block
