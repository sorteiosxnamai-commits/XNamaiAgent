"""O capability catalog é registro técnico neutro: sem fornecedor e sem sorteio."""

from app.capability_catalog import (
    build_capability_catalog,
    format_capability_catalog_for_prompt,
)


def test_catalog_has_no_vendor_name():
    rendered = format_capability_catalog_for_prompt()
    for vendor in ("Tray", "tray", "NewStore", "newstore"):
        assert vendor not in rendered


def test_catalog_has_no_raffle_capabilities():
    catalog = build_capability_catalog()
    assert catalog.get("raffle_capabilities") in (None, [])
    assert "sorteio" not in format_capability_catalog_for_prompt().lower()
    assert not [item for item in catalog["apis"] if item["domain"] != "commerce"]


def test_anti_hallucination_rules_are_preserved():
    policies = " ".join(build_capability_catalog()["policy"])
    assert "Nunca inventar produto" in policies
    assert "fatos comerciais" in policies
    assert "payment_url" in policies
    assert "Não afirmar ausência de pedido" in policies


def test_retryable_classification_still_present():
    catalog = build_capability_catalog()
    by_name = {item["name"]: item for item in catalog["apis"]}
    assert by_name["search_products"]["retryable"] is True
    assert by_name["create_order"]["retryable"] is False


def test_catalog_names_match_commerce_boundary():
    from app.commerce.tools import RETRYABLE_TOOL_NAMES, TOOL_REGISTRY

    catalog = build_capability_catalog()
    assert set(catalog["commerce_apis"]) == set(TOOL_REGISTRY["commerce"])
    assert set(catalog["retryable_apis"]) == set(RETRYABLE_TOOL_NAMES)
