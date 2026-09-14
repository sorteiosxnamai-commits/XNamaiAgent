"""Conversor de rascunho -> payload Mercos v2. Funcao pura, nenhum POST.

A validacao acontece ANTES de qualquer envio: um campo faltando ou ambiguo vira
`OrderDraftError` aqui, e nao pedido errado no ERP do cliente.
"""

from __future__ import annotations

import pytest

from app.commerce.mercos.order_payload import (
    CommerceOrderDraft,
    CommerceOrderDraftItem,
    OrderDraftError,
    build_order_payload,
)


def _item(**overrides):
    fields = {"product_id": "1001", "unit_price": 149.90, "quantity": 2}
    fields.update(overrides)
    return CommerceOrderDraftItem(**fields)


def _draft(**overrides):
    fields = {
        "customer_id": "2002",
        "issued_at": "2026-03-01T12:00:00",
        "items": [_item()],
        "payment_condition_id": "12",
    }
    fields.update(overrides)
    return CommerceOrderDraft(**fields)


# --- caminho valido ---------------------------------------------------------


def test_valid_draft_becomes_the_documented_payload():
    payload = build_order_payload(_draft())
    assert payload == {
        "cliente_id": "2002",
        "data_emissao": "2026-03-01T12:00:00",
        "condicao_pagamento_id": "12",
        "itens": [{"produto_id": "1001", "preco_tabela": 149.90, "quantidade": 2.0}],
    }


def test_payment_condition_by_text_is_accepted():
    payload = build_order_payload(
        _draft(payment_condition_id=None, payment_condition="30/60")
    )
    assert payload["condicao_pagamento"] == "30/60"
    assert "condicao_pagamento_id" not in payload


def test_payload_carries_no_field_beyond_the_contract():
    """Campo extra num POST vira pedido errado. So o documentado sai daqui."""
    payload = build_order_payload(_draft())
    assert set(payload) == {"cliente_id", "data_emissao", "condicao_pagamento_id", "itens"}
    assert set(payload["itens"][0]) == {"produto_id", "preco_tabela", "quantidade"}


def test_multiple_items_are_all_converted():
    payload = build_order_payload(_draft(items=[_item(), _item(product_id="1002")]))
    assert [item["produto_id"] for item in payload["itens"]] == ["1001", "1002"]


# --- obrigatorios -----------------------------------------------------------


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_customer_id_is_required(missing):
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(customer_id=missing))
    assert exc.value.field_name == "cliente_id"


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_issue_date_is_required(missing):
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(issued_at=missing))
    assert exc.value.field_name == "data_emissao"


def test_order_without_items_is_rejected():
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(items=[]))
    assert exc.value.field_name == "itens"


# --- exclusividade da condicao de pagamento ---------------------------------


def test_both_payment_conditions_is_rejected():
    """Ambiguidade no preco: a Mercos precisa de UMA condicao, nao de duas."""
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(payment_condition_id="12", payment_condition="30/60"))
    assert exc.value.field_name == "condicao_pagamento"
    assert "nunca os dois" in str(exc.value)


def test_no_payment_condition_is_rejected():
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(payment_condition_id=None, payment_condition=None))
    assert exc.value.field_name == "condicao_pagamento"


def test_blank_payment_condition_counts_as_absent():
    with pytest.raises(OrderDraftError):
        build_order_payload(_draft(payment_condition_id="  ", payment_condition=""))


# --- item -------------------------------------------------------------------


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_item_product_id_is_required(missing):
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(items=[_item(product_id=missing)]))
    assert exc.value.field_name == "produto_id"


@pytest.mark.parametrize("bad", [0, -1, None])
def test_item_quantity_must_be_positive(bad):
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(items=[_item(quantity=bad)]))
    assert exc.value.field_name == "quantidade"


@pytest.mark.parametrize("bad", [0, -5.5, None])
def test_item_price_must_be_positive(bad):
    """Preco zero num pedido B2B nao e desconto: e erro de montagem."""
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(items=[_item(unit_price=bad)]))
    assert exc.value.field_name == "preco_tabela"


def test_non_numeric_quantity_is_rejected():
    with pytest.raises(OrderDraftError) as exc:
        build_order_payload(_draft(items=[_item(quantity="duas")]))
    assert exc.value.field_name == "quantidade"


# --- fronteira --------------------------------------------------------------


def test_building_a_payload_performs_no_io():
    """O conversor e puro: nao existe caminho de rede a partir dele."""
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app" / "commerce" / "mercos" / "order_payload.py"
    ).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not ({"httpx", "requests", "asyncio"} & imported)
