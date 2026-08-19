"""Catalog index repository + variant key tests (no live DB required)."""

from __future__ import annotations

import pytest

from app.catalog_index import to_canonical_item
from app.catalog_index_repository import CatalogIndexRepository, make_catalog_item_key
from app.fact_authority import catalog_item_key_for


def test_make_catalog_item_key_stable():
    assert make_catalog_item_key("99") == "product:99"
    assert make_catalog_item_key("99", "blue") == "variant:blue"
    assert catalog_item_key_for("99", "blue") == "variant:blue"


def test_to_canonical_item_sets_variant_key():
    item = to_canonical_item(
        {"id": "1", "variant_id": "v-a", "name": "Relogio Azul", "price": 10},
        tenant_id="newstore",
    )
    assert item is not None
    assert item.catalog_item_key == "variant:v-a"


def test_to_canonical_item_product_key_without_variant():
    item = to_canonical_item(
        {"id": "1", "name": "Relogio", "price": 10},
        tenant_id="t1",
    )
    assert item is not None
    assert item.catalog_item_key == "product:1"
    assert item.tenant_id == "t1"


def test_repository_requires_tenant_id():
    repo = CatalogIndexRepository()
    with pytest.raises(ValueError, match="tenant_id"):
        repo.search_exact(tenant_id="", ean="123")


def test_three_variants_get_distinct_keys():
    keys = {
        to_canonical_item(
            {"id": "p1", "variant_id": vid, "name": f"V {vid}"},
            tenant_id="newstore",
        ).catalog_item_key
        for vid in ("v1", "v2", "v3")
    }
    assert keys == {"variant:v1", "variant:v2", "variant:v3"}
