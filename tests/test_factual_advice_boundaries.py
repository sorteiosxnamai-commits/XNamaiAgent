"""General advice and institutional policies never become order/customer evidence."""
import pytest

from app.agent_contracts import build_agent_decision
from app.factual_validator import validate_factual_response
from app.models import AgentResult, IncomingMessage
from app.published_knowledge import bind_publication, reset_publication

POLICY = "Os preços do catálogo são exclusivos para membros do Club Xnamai. Para quem não é membro, há um acréscimo de 15%."


def report(text, *, data=None, metadata=None):
    result = AgentResult(reply_text=text, intent="commerce", commercial_data=data or {},
        response_metadata={"domain": "commerce", "response_source": "consultative_openai",
                           "informational_only": True, **(metadata or {})})
    decision = build_agent_decision(IncomingMessage(text="Pode explicar?"), result, openai_call_count=1)
    return validate_factual_response(result, decision=decision, mode="enforce")


@pytest.fixture
def pricing():
    token = bind_publication({"knowledge_documents": [{"id": "pricing", "topic": "catalog_pricing",
        "status": "approved", "content": POLICY}]})
    yield
    reset_publication(token)


@pytest.mark.parametrize("text", [
    "Se um item recebe pedidos frequentes, priorize a reposição dele.",
    "Quando um item recebe pedidos frequentes, acompanhe o giro.",
    "Para uma loja pequena, comece com poucos itens e acompanhe os pedidos.",
    "Um item com giro frequente ajuda a planejar os pedidos da loja.",
    "Você pode consultar os itens do pedido no catálogo.",
])
@pytest.mark.parametrize("topic", ["commercial_guidance", "order_status"])
def test_general_demand_advice_does_not_claim_order_contents(text, topic):
    assert report(text, metadata={"active_topic": topic}).valid


@pytest.mark.parametrize("text", [
    "Seu pedido tem 3 itens.", "No pedido apareceu só um item.",
    "Seu pedido: 2 itens.", "Seu pedido contém fones e cabos.",
    "Os itens do seu pedido são fones e cabos.", "Encontrei os itens do pedido: fones e cabos.",
    "Se um item recebe pedidos frequentes, reponha. Seu pedido contém 2 itens.",
])
def test_order_contents_need_order_evidence_even_when_catalog_exists(text):
    verdict = report(text, data={"products": [{"id": "fone", "name": "Fone"}]})
    assert any(v.reason == "order_contents_missing_verified_items" for v in verdict.violations)


def test_contextual_count_without_repeated_order_word_remains_guarded():
    verdict = report("Apareceu só um item.", metadata={"active_topic": "order_status"})
    assert any(v.reason == "order_contents_missing_verified_items" for v in verdict.violations)


def test_verified_count_must_match_order_items():
    evidence = {"items_confirmed": True, "items": [{"id": "a"}, {"id": "b"}]}
    assert report("Seu pedido tem 2 itens.", data=evidence).valid
    verdict = report("Seu pedido tem 2 itens. São 3 itens.", data=evidence)
    assert any(v.reason == "order_item_count_mismatch" for v in verdict.violations)


@pytest.mark.parametrize("text", [
    POLICY,
    "Para não membros, há acréscimo de 15%.",
    "Quem não é membro do Club paga 15% a mais.",
    "Para quem não é membro, os preços do catálogo recebem um acréscimo de 15%.",
    "Clientes não membros pagam 15% a mais sobre os valores do catálogo.",
    "Para não membros, aplica-se um adicional de 15% sobre os preços exibidos no catálogo.",
    "Se você não for membro, há acréscimo de 15% sobre os preços do catálogo.",
    "Quem não possui assinatura ativa do Club paga 15 por cento a mais.",
    "Para não membros, há acréscimo de **15,0%**.",
    "O acréscimo para não membros é de 15%.",
    "Para não membros, o valor do catálogo é acrescido de 15%.",
    "Não membros pagam o preço do catálogo mais 15%.",
    "Para não membros: +15% sobre o preço do catálogo.",
    "Quem não é do Club paga 15% a mais.",
    "Quem não faz parte do Club XNamai paga 15% a mais.",
    "Sem Club, os preços do catálogo têm acréscimo de 15%.",
    "Sem o Club Xnamai, o valor é 15% maior.",
    "Para não associados ao Club, há um adicional de 15%.",
    "Clientes não assinantes pagam 15% a mais.",
    "Se você não for associado, há acréscimo de 15%.",
])
def test_correct_institutional_paraphrases_are_supported(pricing, text):
    verdict = report(text)
    assert verdict.valid, verdict.violations
    assert any(c.reason == "published_catalog_pricing_supported" for c in verdict.supported_claims)


def test_simple_followup_can_repeat_policy_with_general_club_aliases(pricing):
    verdict = report("Para quem não é membro, há acréscimo de 15%. "
        "Em geral, quem não é do Club paga 15% a mais.")
    # Discourse framing is not a new rule or a customer membership claim.
    assert verdict.valid, verdict.violations
    assert len([claim for claim in verdict.supported_claims if claim.claim == "catalog_surcharge"]) == 2


@pytest.mark.parametrize("text", [
    "Para não membros, há acréscimo de 25%.",
    "Para membros, há acréscimo de 15%.",
    "Quem não é membro recebe desconto de 15%.",
    "Para não membros, não há acréscimo de 15%.",
    "Para não membros, há acréscimo de até 15%.",
    "Para não membros, há acréscimo de -15%.",
    "Para não membros, o seu pedido tem acréscimo de 15%.",
    "Seu pedido tem acréscimo de 15% porque você não é membro.",
    "Você vai pagar 15% a mais neste pedido.",
    "Para não membros, esse fone recebe acréscimo de 15%.",
    "Para não membros, há acréscimo de 15% e para membros também.",
    "Para não membros, há acréscimo de 15% e mais 25%.",
    POLICY + " Você paga 15% a mais neste pedido.",
    "Não membros pagam sem acréscimo.",
    "Você é membro do Club.",
    "Como você não é membro, seu cadastro recebe acréscimo de 15%.",
    "Sua assinatura está ativa.",
    "Não consigo consultar o sistema, mas sua assinatura está ativa.",
    POLICY + " Seu cadastro está confirmado como membro.",
    "Quem não é do Club recebe desconto de 15%.",
    "Sem Club, os preços têm acréscimo de 25%.",
    "Clientes não associados pagam 10% a mais.",
    "Sem Club, seu pedido terá acréscimo de 15%.",
    "Sem Club, esse fone tem acréscimo de 15%.",
    "Você não é do Club, então paga 15% a mais.",
    "Você já é associado ao Club.",
    "Seu cadastro está associado ao Club.",
])
def test_policy_does_not_authorize_other_percentages_membership_or_customer_adjustments(pricing, text):
    assert not report(text).valid


@pytest.mark.parametrize("text", [
    "Se você já tem assinatura, entre no Club e confira o status.",
    "Não consigo confirmar se sua assinatura está ativa.",
    "A política não comprova que você é membro.",
    "Você já é membro do Club?",
])
def test_membership_explanation_is_not_a_membership_confirmation(pricing, text):
    assert report(text).valid


@pytest.mark.parametrize("documents", [
    [],
    [{"id": "draft", "topic": "catalog_pricing", "status": "draft", "content": POLICY}],
    [{"id": "expired", "topic": "catalog_pricing", "status": "approved", "content": POLICY,
      "valid_until": "2020-01-01T00:00:00Z"}],
    [{"id": str(i), "topic": "catalog_pricing", "status": "approved", "content": POLICY} for i in range(2)],
])
def test_paraphrase_needs_unambiguous_current_approved_policy(documents):
    token = bind_publication({"knowledge_documents": documents})
    try:
        assert not report("Quem não é membro paga 15% a mais.").valid
    finally:
        reset_publication(token)


def test_approved_percentage_cannot_supply_order_money_or_change_real_total(pricing):
    verdict = report("Quem não é membro paga 15% a mais. Seu pedido custa R$ 920,00.",
        data={"order_total": "800.00"})
    assert any(v.reason == "money_not_present_in_verified_facts" for v in verdict.violations)


def test_general_store_domain_has_same_institutional_pricing_boundaries(pricing):
    assert report("Quem não é membro paga 15% a mais.", metadata={"domain": "store_general"}).valid
    assert not report("Quem não é membro paga 25% a mais.", metadata={"domain": "store_general"}).valid


def test_additional_unparsed_percentage_in_policy_cannot_be_silently_ignored():
    token = bind_publication({"knowledge_documents": [{"id": "pricing", "topic": "catalog_pricing",
        "status": "approved", "content": POLICY + " Para os demais clientes, 25%."}]})
    try:
        assert not report("Quem não é membro paga 15% a mais.").valid
    finally:
        reset_publication(token)


@pytest.mark.parametrize("text", [
    "Nosso catálogo está disponível aqui: https://xnamai.meuspedidos.com.br/",
    "Boa tarde! 😊 O catálogo completo está disponível no link oficial.",
    "Claro, o catálogo digital já está disponível.",
    "📚 *Catálogo oficial*: https://xnamai.meuspedidos.com.br/\nCatálogo disponível para consulta.",
    "O site da XNamai está disponível para consulta.",
    "O acesso ao catálogo está disponível pelo link oficial.",
    "O link do catálogo está disponível aqui.",
    "O portal está indisponível agora.",
    "Nosso site não está disponível no momento.",
])
def test_catalog_site_and_access_availability_are_not_product_stock(text):
    verdict = report(text)
    assert verdict.valid, verdict.violations
    assert not any(claim.kind == "stock" for claim in verdict.supported_claims)


def test_live_catalog_registration_journey_stock_false_positive(pricing):
    """Reproduce the observed live failure's institutional claims and metadata."""
    verdict = report("Boa tarde! 😊 Nosso catálogo está disponível aqui: "
        "https://xnamai.meuspedidos.com.br/\n\n"
        "Os preços do catálogo são exclusivos para membros do Club Xnamai. "
        "Para quem não é membro, há um acréscimo de 15%.\n"
        "Você quer se cadastrar com CPF ou CNPJ?", metadata={"used_commerce_provider": False})
    assert verdict.valid, verdict.violations
    assert any(claim.reason == "url_supported" for claim in verdict.supported_claims)
    assert any(claim.reason == "published_catalog_pricing_supported" for claim in verdict.supported_claims)


@pytest.mark.parametrize("text", [
    "O fone está disponível no catálogo.",
    "O produto do catálogo está disponível.",
    "A caixa de som 120W está disponível no site.",
    "O fone de ouvido está disponível.",
    "Os mouses estão disponíveis no catálogo.",
    "As caixas de som estão esgotadas.",
    "O fone está indisponível.",
    "Os produtos estão indisponíveis.",
    "Está disponível no site oficial.",
    "O catálogo está disponível e o fone está disponível também.",
    "O catálogo está disponível. O fone também está disponível.",
    "O catálogo está disponível, com fones disponíveis para comprar.",
    "O link do catálogo está disponível. A caixa de som está em estoque.",
    "O catálogo está disponível, os fones estão prontos para envio.",
    "O catálogo de produtos em estoque está disponível.",
    "O catálogo está disponível e o mouse está indisponível.",
])
def test_product_or_ambiguous_availability_still_requires_stock_evidence(text):
    verdict = report(text)
    assert any(claim.reason == "stock_missing_evidence" for claim in verdict.violations)


@pytest.mark.parametrize("stock", [0, 7])
def test_institutional_availability_does_not_conflict_with_unrelated_inventory(stock):
    verdict = report("O catálogo está disponível. O site está indisponível.",
        data={"products": [{"id": "fone", "name": "Fone", "stock": stock}]},
        metadata={"used_commerce_provider": True})
    assert verdict.valid, verdict.violations
    assert not any(claim.kind == "stock" for claim in verdict.supported_claims)


@pytest.mark.parametrize("stock,text,valid", [
    (7, "O catálogo está disponível. O fone está disponível.", True),
    (0, "O catálogo está disponível. O fone está disponível.", False),
    (7, "O site está disponível. Os fones estão esgotados.", False),
    (0, "O site está disponível. Os fones estão esgotados.", True),
])
def test_mixed_reply_keeps_current_product_inventory_validation(stock, text, valid):
    verdict = report(text, data={"products": [{"id": "fone", "name": "Fone", "stock": stock}]},
        metadata={"used_commerce_provider": True})
    assert verdict.valid is valid, verdict.violations
    if not valid:
        assert any(claim.reason == "stock_claim_conflicts_with_evidence" for claim in verdict.violations)
