from app.fact_sources import (
    FactSource,
    StructuredFact,
    infer_source_for_payload_key,
    preferred_fact,
)


def test_security_and_deterministic_outrank_persona_and_memory():
    facts = [
        StructuredFact(
            source=FactSource.CUSTOMER_MEMORY,
            key="price",
            value="1.00",
            entity_type="price",
        ),
        StructuredFact(
            source=FactSource.APPROVED_PERSONA,
            key="price",
            value="2.00",
            entity_type="price",
        ),
        StructuredFact(
            source=FactSource.TRAY_ADAPTER,
            key="price",
            value="199.90",
            entity_type="price",
            entity_id="p1",
        ),
    ]
    chosen = preferred_fact(facts, key="price")
    assert chosen is not None
    assert chosen.source == FactSource.TRAY_ADAPTER
    assert chosen.value == "199.90"


def test_tray_live_outranks_tray_adapter_and_local_db():
    facts = [
        StructuredFact(
            source=FactSource.LOCAL_DATABASE,
            key="price",
            value="150.00",
            entity_type="price",
        ),
        StructuredFact(
            source=FactSource.TRAY_ADAPTER,
            key="price",
            value="180.00",
            entity_type="price",
        ),
        StructuredFact(
            source=FactSource.TRAY_LIVE,
            key="price",
            value="199.90",
            entity_type="price",
        ),
    ]
    chosen = preferred_fact(facts, key="price", entity_type="price")
    assert chosen is not None
    assert chosen.source == FactSource.TRAY_LIVE



def test_infer_source_maps_catalog_and_state_keys():
    assert (
        infer_source_for_payload_key("current_price", used_tray=True)
        == FactSource.TRAY_ADAPTER
    )
    assert (
        infer_source_for_payload_key("pending_action", from_commerce_state=True)
        == FactSource.COMMERCE_STATE
    )
    assert (
        infer_source_for_payload_key("coupon_balance", from_local_db=True)
        == FactSource.LOCAL_DATABASE
    )
