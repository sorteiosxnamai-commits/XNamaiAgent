import time

import pytest

from app.product_snapshot import (
    ProductSnapshot,
    get_product_snapshot_cache,
    product_dict_to_snapshot,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    cache = get_product_snapshot_cache()
    cache.clear()
    cache.hits = cache.misses = cache.stale_served = cache.invalidations = 0
    yield
    cache.clear()


def test_cache_hit_miss_expiry_and_tenant_isolation():
    cache = get_product_snapshot_cache()
    snap_a = product_dict_to_snapshot(
        {
            "id": "1",
            "name": "Seastar",
            "current_price": "1990.00",
            "stock": 2,
            "available": True,
        },
        tenant_id="store-a",
        match_kind="exact",
    )
    cache.put(snap_a, kind="product", ttl_seconds=0.2, payload={"id": "1", "name": "Seastar"})
    assert cache.get(tenant_id="store-a", kind="product", entity_id="1") is not None
    assert cache.get(tenant_id="store-b", kind="product", entity_id="1") is None
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1
    time.sleep(0.25)
    assert cache.get(tenant_id="store-a", kind="product", entity_id="1") is None
    stale = cache.get(
        tenant_id="store-a",
        kind="product",
        entity_id="1",
        allow_stale=True,
    )
    assert stale is not None
    assert stale.product_id == "1"


def test_stale_allowed_for_non_critical_and_forbidden_for_stock_path():
    cache = get_product_snapshot_cache()
    snap = ProductSnapshot(
        product_id="9",
        name="X",
        available=True,
        stock_quantity=1,
        tenant_id="store",
    )
    cache.put(snap, kind="image", ttl_seconds=0.01, payload={"id": "9"})
    time.sleep(0.02)
    assert (
        cache.get(tenant_id="store", kind="image", entity_id="9", allow_stale=True)
        is not None
    )
    cache.put(snap, kind="stock", ttl_seconds=0.01, payload={"stock": 1})
    time.sleep(0.02)
    assert (
        cache.get(tenant_id="store", kind="stock", entity_id="9", allow_stale=False)
        is None
    )


def test_invalidation_and_exact_vs_similar():
    cache = get_product_snapshot_cache()
    exact = product_dict_to_snapshot(
        {"id": "2", "name": "Seastar", "model": "Seastar"},
        tenant_id="store",
        match_kind="exact",
    )
    similar = product_dict_to_snapshot(
        {"id": "3", "name": "PRC"},
        tenant_id="store",
        match_kind="similar",
    )
    cache.put(exact, kind="product", ttl_seconds=30, payload={"id": "2"})
    cache.put(similar, kind="product", ttl_seconds=30, payload={"id": "3"})
    cache.invalidate(tenant_id="store", entity_id="2", kinds=["product"])
    assert cache.get(tenant_id="store", kind="product", entity_id="2") is None
    kept = cache.get(tenant_id="store", kind="product", entity_id="3")
    assert kept is not None
    assert kept.match_kind == "similar"
