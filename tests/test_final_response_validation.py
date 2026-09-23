"""B9 — validacao final: fato comercial so com evidencia do turno; nada de estilo."""

from __future__ import annotations

import pytest

from app.agent_contracts import build_agent_decision
from app.factual_validator import apply_factual_validation, validate_factual_response
from app.models import AgentResult, IncomingMessage


def _commerce(reply: str, commercial_data: dict | None = None, **metadata) -> AgentResult:
    return AgentResult(
        reply_text=reply,
        intent="commerce",
        commercial_data=commercial_data or {"products": [{"id": "1", "name": "Cabo USB-C", "current_price": "19.90"}]},
        response_metadata={"domain": "commerce", "used_commerce_provider": True, **metadata},
    )


def _report(result: AgentResult):
    decision = build_agent_decision(IncomingMessage(channel="whatsapp", text="?"), result, openai_call_count=1)
    return validate_factual_response(result, decision=decision, mode="enforce")


@pytest.mark.parametrize(
    ("reply", "kind", "reason"),
    [
        ("O Cabo USB-C sai em 10x sem juros.", "condition", "installments_without_provider_evidence"),
        ("O Cabo USB-C tem frete grátis.", "condition", "free_shipping_without_provider_evidence"),
        ("Hoje o Cabo USB-C está com 15% de desconto.", "promo", "percent_discount_without_promotional_price_evidence"),
        ("O Cabo USB-C é pronta entrega.", "availability", "immediate_delivery_without_provider_evidence"),
        ("Segue a foto do Cabo USB-C.", "image", "image_claimed_without_outbound_image"),
    ],
)
def test_unsupported_commercial_claim_is_a_violation(reply, kind, reason):
    report = _report(_commerce(reply))
    assert not report.valid
    assert {(v.kind, v.reason) for v in report.violations} >= {(kind, reason)}
    assert report.fallback_required


@pytest.mark.parametrize(
    ("reply", "commercial_data", "metadata"),
    [
        ("Sai em 3x sem juros.",
         {"products": [{"id": "1"}], "payment_options": [{"installments": 3, "interest": False}]}, {}),
        ("Esse tem frete grátis.",
         {"products": [{"id": "1"}], "shipping": {"options": [{"name": "PAC", "price": 0}]}}, {}),
        ("Está com 10% de desconto.",
         {"products": [{"id": "1", "current_price": "90.00", "promotional_price": "90.00"}]}, {}),
        ("É pronta entrega.",
         {"products": [{"id": "1"}], "commercial_availability": {"immediate_delivery_supported": True}}, {}),
        ("Segue a foto.", {"products": [{"id": "1"}]}, {"outbound_image_url": "https://cdn.example/p1.jpg"}),
    ],
)
def test_the_same_claims_pass_with_provider_evidence(reply, commercial_data, metadata):
    report = _report(_commerce(reply, commercial_data, **metadata))
    assert not [v for v in report.violations if v.kind in {"condition", "availability", "image", "promo"}], report.violations


@pytest.mark.parametrize(
    "reply",
    [
        "O Kit 2x Cabo USB-C está no catálogo.",
        "Temos o carregador 3x USB com cabo.",
        "Segue o link do produto para você ver as fotos no site.",
    ],
)
def test_product_names_and_plain_mentions_are_not_commercial_claims(reply):
    report = _report(_commerce(reply))
    assert not [v for v in report.violations if v.kind in {"condition", "image", "availability"}]


@pytest.mark.parametrize("reply", ["Dá para pagar em até 6x.", "Sai em 3x de R$ 6,63.", "Pode dividir em 4 vezes."])
def test_real_installment_phrasings_are_still_checked(reply):
    report = _report(_commerce(reply))
    assert any(v.reason == "installments_without_provider_evidence" for v in report.violations)


def test_inconsistent_final_response_is_replaced_before_sending():
    result = _commerce("O Cabo USB-C custa R$ 9,90 e sai em 12x sem juros.")
    decision = build_agent_decision(IncomingMessage(channel="whatsapp", text="?"), result, openai_call_count=1)
    validated = apply_factual_validation(result, decision=decision, mode="enforce")
    assert validated.safety_reason == "factual_validation_failed"
    assert "12x" not in validated.reply_text and "9,90" not in validated.reply_text


def test_validator_is_not_a_style_editor():
    """Abertura, tom, CTA e numero de perguntas nao sao fatos: passam intactos."""
    reply = "Claro! Com certeza, posso ajudar. Quer ver outras cores? Posso te mostrar mais?"
    result = _commerce(reply)
    decision = build_agent_decision(IncomingMessage(channel="whatsapp", text="?"), result, openai_call_count=1)
    validated = apply_factual_validation(result, decision=decision, mode="enforce")
    assert validated.reply_text == reply
    assert validated.response_metadata["factual_validation"]["valid"] is True
