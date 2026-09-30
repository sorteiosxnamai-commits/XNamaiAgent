import pytest

from app.published_knowledge import bind_publication, published_policy, reset_publication
from app.store_guidance import build_store_guidance
from app.published_knowledge import supports_policy_line


def test_guidance_uses_current_published_minimum_and_resets_scope():
    token = bind_publication({"knowledge_documents": [
        {"id": "minimum", "topic": "minimum_order", "status": "approved",
         "content": "O pedido mínimo é de R$ 950,00."}
    ]})
    try:
        result = build_store_guidance("Quero saber sobre seus produtos, atacado e varejo e preços")
        assert result is not None
        assert "R$ 950,00" in result.reply_text
        assert "R$ 800,00" not in result.reply_text
    finally:
        reset_publication(token)
    assert published_policy("minimum_order") is None


@pytest.mark.parametrize("documents", [
    [],
    [{"id": "minimum", "topic": "minimum_order", "status": "draft", "content": "R$ 950"}],
    [{"id": "minimum", "topic": "minimum_order", "valid_until": "2020-01-01T00:00:00Z", "content": "R$ 950"}],
    [{"id": "one", "topic": "minimum_order", "content": "R$ 950"},
     {"id": "two", "topic": "minimum_order", "content": "R$ 500"}],
])
def test_unpublished_expired_or_ambiguous_policy_is_not_used(documents):
    assert published_policy("minimum_order", documents=documents) is None


def test_policy_money_does_not_authorize_product_price_with_same_amount():
    token = bind_publication({"knowledge_documents": [{"id": "minimum", "topic": "minimum_order",
            "status": "approved", "content": "O pedido mínimo é R$ 950,00."}]})
    try:
        assert supports_policy_line("O pedido mínimo é R$ 950,00.")
        assert not supports_policy_line("O Fone custa R$ 950,00.")
        assert not supports_policy_line("O pedido mínimo é R$ 950,00. O fone também custa R$ 950,00.")
    finally:
        reset_publication(token)
