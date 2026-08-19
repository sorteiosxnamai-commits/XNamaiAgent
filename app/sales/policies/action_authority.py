from __future__ import annotations

from app.commerce_context import CommerceConversationState
from app.models import AgentResult


def purchase_product_required_result(
    state: CommerceConversationState,
) -> AgentResult:
    ambiguous = bool(state.last_presented_products)
    return AgentResult(
        reply_text=(
            "Confirme qual produto você quer comprar antes de eu preparar o carrinho."
            if ambiguous
            else "Preciso saber qual produto você quer comprar antes de preparar o carrinho."
        ),
        intent="commerce",
        handoff_required=False,
        safety_reason="product_ambiguous" if ambiguous else "no_cart_no_product",
        commercial_data={
            "products": [
                item.model_dump(mode="json")
                for item in state.last_presented_products[:3]
            ],
            "cart": {"status": "product_required"},
            "action_guard": {
                "action": "create_cart",
                "allowed": False,
                "blocking_reason": (
                    "product_selection_required"
                    if ambiguous
                    else "product_target_missing"
                ),
            },
        },
        response_metadata={"domain": "commerce"},
    )
