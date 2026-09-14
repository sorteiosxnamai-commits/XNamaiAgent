"""Normalizacao Mercos -> contratos neutros, com o schema oficial.

A regra que domina este arquivo: **ausencia nao e zero**. Um `preco_tabela`
ausente que virasse `0` faria o agente anunciar produto de graca; um
`saldo_estoque` ausente que virasse `0` faria ele negar venda de item que existe.
Os dois parecem fato e custam caro.
"""

from __future__ import annotations

import pytest

from app.commerce.mercos.normalizer import (
    ORDER_BILLING_STATUS,
    ORDER_STATUS,
    compute_available,
    normalize_customer,
    normalize_order,
    normalize_product,
)
from tests.fixtures import mercos_payloads as fx


# ===========================================================================
# PRODUTO
# ===========================================================================


def test_full_product_maps_every_documented_field():
    p = normalize_product(fx.product())
    assert p.external_id == "1001"
    assert p.provider == "mercos"
    assert p.reference == "REF-1001"
    assert p.name == "Produto Sintetico A"
    assert p.price == 149.90
    assert p.stock == 7
    assert p.active is True
    assert p.excluded is False
    assert p.category_id == "55"
    assert p.unit == "UN"
    assert p.changed_at == "2026-03-01T10:00:00"


def test_product_without_id_is_discarded_not_invented():
    assert normalize_product(fx.product(id=...)) is None
    assert normalize_product(fx.product(id=None)) is None
    assert normalize_product({}) is None
    assert normalize_product(None) is None


# --- ausencia nao e zero ---------------------------------------------------


@pytest.mark.parametrize("missing", [..., None, ""])
def test_absent_price_is_none_never_zero(missing):
    assert normalize_product(fx.product(preco_tabela=missing)).price is None


@pytest.mark.parametrize("missing", [..., None, ""])
def test_absent_stock_is_none_never_zero(missing):
    assert normalize_product(fx.product(saldo_estoque=missing)).stock is None


@pytest.mark.parametrize("missing", [..., None])
def test_absent_active_is_none_never_false(missing):
    assert normalize_product(fx.product(ativo=missing)).active is None


def test_zero_is_a_real_value_and_survives():
    """O oposto do bug anterior: zero legitimo nao pode virar None."""
    p = normalize_product(fx.product(preco_tabela=0, saldo_estoque=0))
    assert p.price == 0.0
    assert p.stock == 0


def test_price_accepts_string_and_comma_decimal():
    assert normalize_product(fx.product(preco_tabela="1.234,50")).price == 1234.50
    assert normalize_product(fx.product(preco_tabela="99.9")).price == 99.9


def test_unparseable_price_is_none_not_zero():
    assert normalize_product(fx.product(preco_tabela="sob consulta")).price is None


# --- available: regra estreita e documentada -------------------------------


def test_available_true_only_with_full_evidence():
    p = normalize_product(fx.product(ativo=True, excluido=False, saldo_estoque=3))
    assert p.available is True


def test_available_false_when_evidence_is_complete_and_stock_is_zero():
    p = normalize_product(fx.product(ativo=True, excluido=False, saldo_estoque=0))
    assert p.available is False


@pytest.mark.parametrize(
    "active,excluded,stock",
    [
        (None, False, 3),   # sem ativo
        (True, None, 3),    # sem excluido
        (True, False, None),  # sem saldo
        (False, False, 3),  # inativo: semantica de venda nao confirmada
        (True, True, 3),    # excluido
    ],
)
def test_available_is_none_without_complete_evidence(active, excluded, stock):
    """`None` = nao confirmado. Nunca afirma disponivel NEM indisponivel."""
    assert compute_available(active=active, excluded=excluded, stock=stock) is None


def test_available_never_guessed_from_price_or_category():
    """Produto existir e ter preco nao diz nada sobre estoque."""
    p = normalize_product(fx.product(saldo_estoque=..., preco_tabela=199.0, ativo=True))
    assert p.available is None
    assert p.stock is None


# --- projecao para o modelo -------------------------------------------------


def test_product_for_model_never_leaks_raw():
    row = fx.product(observacoes="anotacao interna confidencial")
    payload = normalize_product(row).for_model()
    assert "raw" not in payload
    assert "anotacao interna confidencial" not in str(payload)
    assert "peso_bruto" not in payload


def test_product_raw_is_preserved_for_the_local_index():
    p = normalize_product(fx.product())
    assert p.raw["codigo_ncm"] == "00000000"


# ===========================================================================
# CLIENTE
# ===========================================================================


def test_full_customer_maps_every_documented_field():
    c = normalize_customer(fx.customer())
    assert c.external_id == "2002"
    assert c.legal_name == "Empresa Sintetica LTDA"
    assert c.display_name == "Empresa Sintetica"
    assert c.person_type == "PJ"
    assert c.document == "00000000000000"
    assert c.emails == ["contato@example.invalid"]
    assert c.phones == ["+550000000000"]
    assert c.blocked is False
    assert c.excluded is False


def test_customer_without_id_is_discarded():
    assert normalize_customer(fx.customer(id=...)) is None


def test_customer_contacts_accept_list_of_dicts():
    c = normalize_customer(
        fx.customer(emails=[{"email": "a@example.invalid"}], telefones=[{"numero": "+551"}])
    )
    assert c.emails == ["a@example.invalid"]
    assert c.phones == ["+551"]


def test_absent_contacts_become_empty_not_fake():
    c = normalize_customer(fx.customer(emails=..., telefones=None))
    assert c.emails == []
    assert c.phones == []


def test_customer_for_model_never_exposes_pii():
    """O modelo sabe QUE existe contato, nunca QUAL e."""
    payload = normalize_customer(fx.customer()).for_model()
    rendered = str(payload)
    for pii in ("00000000000000", "contato@example.invalid", "+550000000000"):
        assert pii not in rendered
    assert payload["has_document"] is True
    assert payload["has_email"] is True
    assert payload["has_phone"] is True


def test_customer_repr_never_exposes_pii():
    """Repr vaza para log e traceback: PII fica fora."""
    rendered = repr(normalize_customer(fx.customer()))
    for pii in ("00000000000000", "contato@example.invalid", "+550000000000"):
        assert pii not in rendered


# ===========================================================================
# PEDIDO
# ===========================================================================


def test_full_order_maps_every_documented_field():
    o = normalize_order(fx.order())
    assert o.external_id == "3003"
    assert o.number == "PED-3003"
    assert o.total == 304.80
    assert o.shipping_value == 25.00
    assert o.customer_id == "2002"
    assert o.payment_condition == "30/60"
    assert o.issued_at == "2026-03-01T12:00:00"
    assert len(o.items) == 1


@pytest.mark.parametrize("raw,label", sorted(ORDER_STATUS.items()))
def test_official_order_status_values(raw, label):
    o = normalize_order(fx.order(status=raw))
    assert o.status == label
    assert o.status_raw == raw


@pytest.mark.parametrize("raw,label", sorted(ORDER_BILLING_STATUS.items()))
def test_official_billing_status_values(raw, label):
    o = normalize_order(fx.order(status_faturamento=raw))
    assert o.billing_status == label
    assert o.billing_status_raw == raw


def test_unknown_status_keeps_raw_and_refuses_to_label_it():
    """Codigo novo da Mercos nao pode virar rotulo inventado."""
    o = normalize_order(fx.order(status=9))
    assert o.status is None
    assert o.status_raw == 9


def test_order_items_are_normalized():
    item = normalize_order(fx.order()).items[0]
    assert item.product_id == "1001"
    assert item.product_reference == "REF-1001"
    assert item.quantity == 2
    assert item.unit_price == 149.90
    assert item.net_price == 139.90
    assert item.subtotal == 279.80


def test_order_without_items_is_valid():
    o = normalize_order(fx.order(itens=[]))
    assert o.items == []


def test_excluded_items_are_hidden_from_the_model_but_kept_in_the_contract():
    o = normalize_order(fx.order(itens=[fx.order_item(excluido=True), fx.order_item(id=5002)]))
    assert len(o.items) == 2
    assert len(o.for_model()["items"]) == 1


def test_order_for_model_never_exposes_customer_registration_data():
    """Razao social e CNPJ do cliente do pedido nao sobem para o modelo."""
    payload = normalize_order(fx.order()).for_model()
    rendered = str(payload)
    for pii in ("Empresa Sintetica LTDA", "00000000000000"):
        assert pii not in rendered


def test_partial_order_payload_does_not_invent_totals():
    o = normalize_order(fx.order(total=..., valor_frete=...))
    assert o.total is None
    assert o.shipping_value is None
