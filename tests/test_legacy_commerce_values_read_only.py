"""Valores do fornecedor anterior: LIDOS enquanto houver dado gravado, nunca produzidos.

NEW WRITE -> somente ``commerce_*``
OLD DATA  -> ``tray_live`` / ``tray_search`` / ``tray_api`` continuam legiveis
             com a MESMA autoridade dos nomes neutros.

A compatibilidade de leitura so sai depois da migration de dados descrita em
``docs/legacy_commerce_values_migration.md``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGACY_VALUES = {"tray_live", "tray_search", "tray_api"}

#: Unicos lugares onde o valor antigo pode aparecer como literal: as
#: DEFINICOES de leitura compativel.
READ_COMPAT_DEFINITIONS = {
    "app/fact_sources.py",  # LIVE/SEARCH_PROVIDER_SOURCE_VALUES
    "app/catalog_index.py",  # FactualSource (Literal aceito na leitura)
    "app/story_commercial_policy.py",  # LIVE_EVIDENCE_SOURCES / Literal de ProductEvidence
}


def _string_literals(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_no_runtime_module_writes_a_legacy_value():
    offenders = []
    for base in ("app", "api", "scripts"):
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "__pycache__" in path.parts or rel in READ_COMPAT_DEFINITIONS:
                continue
            found = _string_literals(path) & LEGACY_VALUES
            if found:
                offenders.append(f"{rel}: {sorted(found)}")
    assert not offenders, f"valor legado produzido fora da leitura compativel: {offenders}"


def test_read_compat_definitions_are_still_where_the_allowlist_says():
    for rel in READ_COMPAT_DEFINITIONS:
        assert _string_literals(REPO_ROOT / rel) & LEGACY_VALUES, rel


# --- NEW WRITE -------------------------------------------------------------


def test_new_catalog_writes_use_neutral_names():
    from app.catalog_index import CanonicalCatalogItem, to_canonical_item
    from app.commerce.mercos.catalog import CATALOG_FACTUAL_SOURCE

    assert CATALOG_FACTUAL_SOURCE == "commerce_search"
    assert CanonicalCatalogItem.model_fields["factual_source"].default == "commerce_search"
    item = to_canonical_item({"id": "1", "name": "Cabo USB-C"}, tenant_id="xnamai")
    assert item is not None and item.factual_source == "commerce_search"


def test_runtime_bootstrap_ddl_defaults_to_neutral_name():
    source = (REPO_ROOT / "app" / "db.py").read_text(encoding="utf-8")
    assert "DEFAULT 'commerce_search'" in source
    assert "DEFAULT 'tray_search'" not in source


def test_new_story_evidence_uses_neutral_name():
    from app.story_commercial_policy import evidence_from_commerce_product

    evidence = evidence_from_commerce_product(
        {"id": "9", "name": "Fone", "price": 10, "stock": 3, "url": "https://x/p/9"},
        tenant_id="xnamai",
    )
    assert evidence.source == "commerce_api"


def test_revalidated_story_product_is_marked_commerce_live():
    source = (REPO_ROOT / "app" / "instagram_story_service.py").read_text(encoding="utf-8")
    assert 'product["_factual_source"] = "commerce_live"' in source


# --- OLD DATA --------------------------------------------------------------


@pytest.mark.parametrize(("legacy", "neutral"), [("tray_live", "commerce_live"), ("tray_search", "commerce_search")])
def test_persisted_legacy_factual_source_keeps_its_authority(legacy, neutral):
    from app.fact_authority import claim_from_product_field
    from app.fact_sources import infer_source_for_payload_key

    assert infer_source_for_payload_key("price", factual_source=legacy) == infer_source_for_payload_key(
        "price", factual_source=neutral
    )
    old = claim_from_product_field({"_factual_source": legacy}, kind="price", key="price", value=10)
    new = claim_from_product_field({"_factual_source": neutral}, kind="price", key="price", value=10)
    assert (old.source, old.revalidation_status, old.confidence) == (
        new.source,
        new.revalidation_status,
        new.confidence,
    )


def test_persisted_legacy_catalog_row_still_validates():
    from app.catalog_index import CanonicalCatalogItem

    for legacy in ("tray_live", "tray_search"):
        item = CanonicalCatalogItem.model_validate(
            {"tenant_id": "xnamai", "product_id": "1", "name": "Cabo", "factual_source": legacy}
        )
        assert item.factual_source == legacy


def test_persisted_legacy_story_evidence_still_authorizes_like_the_neutral_one():
    from app.story_commercial_policy import ProductEvidence

    common = dict(tenant_id="xnamai", product_id="9", price_cents=1000, stock_quantity=2, product_url="https://x/p/9")
    old = ProductEvidence(source="tray_api", **common)
    new = ProductEvidence(source="commerce_api", **common)
    for check in ("authorizes_price", "authorizes_stock", "authorizes_url"):
        assert getattr(old, check)() is getattr(new, check)() is True
