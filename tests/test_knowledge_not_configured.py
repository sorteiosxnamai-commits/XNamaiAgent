"""Gate 5 / Task 10 — knowledge modules must not carry NewStore commercial content."""

from __future__ import annotations

import importlib
import pathlib

import pytest

NEWSTORE_TOKENS = (
    "sorteionewstore",
    "newstorerj",
    "NewStore",
    "newstore",
    "New Store",
    "Felipe Newbold",
    "9949-0859",
)


def _text(relative: str) -> str:
    return pathlib.Path(relative).read_text(encoding="utf-8")


def test_site_knowledge_has_no_newstore_content():
    text = _text("app/site_knowledge.py")
    for token in NEWSTORE_TOKENS:
        assert token not in text, f"site_knowledge.py ainda cita {token!r}"


def test_vip_profiles_has_no_newstore_content():
    text = _text("app/vip_profiles.py")
    for token in NEWSTORE_TOKENS:
        assert token not in text, f"vip_profiles.py ainda cita {token!r}"


def test_store_knowledge_module_is_deleted():
    assert not pathlib.Path("app/store_knowledge.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.store_knowledge")


def test_site_knowledge_exported_names_are_preserved():
    module = importlib.import_module("app.site_knowledge")
    for name in (
        "SITE_URL",
        "STORE_URL",
        "NS_SALES_WHATSAPP",
        "HUMAN_SUPPORT_MESSAGE",
        "TRADE_IN_HANDOFF_MESSAGE",
        "REGISTER_PHONE_MESSAGE",
        "THIRD_PARTY_REFUSAL",
        "CARD_USAGE_TABLE",
        "min_purchase_for_credit_cents",
        "credit_band_for_amount",
        "max_applicable_credit_for_product_cents",
        "format_card_usage_table_text",
        "build_site_knowledge_text",
        "build_rules_reply",
        "build_simulation_reply",
    ):
        assert hasattr(module, name), f"site_knowledge perdeu {name}"


def test_site_knowledge_urls_and_contacts_are_empty():
    from app import site_knowledge

    assert site_knowledge.SITE_URL == ""
    assert site_knowledge.STORE_URL == ""
    assert site_knowledge.NS_SALES_WHATSAPP == ""


def test_credit_table_is_empty_structure():
    from app import site_knowledge

    assert site_knowledge.CARD_USAGE_TABLE == ()
    assert site_knowledge.min_purchase_for_credit_cents(500_00) is None
    assert site_knowledge.credit_band_for_amount(500_00) is None


def test_non_positive_product_guard_is_preserved():
    """Guarda genérica: valor de produto não-positivo nunca aplica crédito."""
    from app import site_knowledge

    assert site_knowledge.max_applicable_credit_for_product_cents(0) == 0
    assert site_knowledge.max_applicable_credit_for_product_cents(-1) == 0
    assert site_knowledge.max_applicable_credit_for_product_cents(10_000_00) == 0


def test_knowledge_texts_say_not_configured():
    from app import site_knowledge

    for builder in (
        site_knowledge.build_site_knowledge_text,
        site_knowledge.build_rules_reply,
        site_knowledge.format_card_usage_table_text,
    ):
        rendered = builder()
        assert isinstance(rendered, str)
        assert "configurad" in rendered.lower(), f"{builder.__name__} deveria dizer 'não configurado'"


def test_vip_registry_is_empty_but_lookup_guards_survive():
    from app import vip_profiles

    assert vip_profiles.VIP_PROFILES == ()
    assert vip_profiles.get_vip_profile(None) is None
    assert vip_profiles.get_vip_profile("") is None
    assert vip_profiles.get_vip_profile("+55 21 96954-4700") is None


def test_vip_nickname_fallback_guard_is_preserved():
    from app.vip_profiles import VipProfile, pick_vip_nickname

    without = VipProfile(phone_suffix="1", full_name="Ana Souza", title="t", nicknames=())
    assert pick_vip_nickname(without, "seed") == "Ana"

    with_nicks = VipProfile(
        phone_suffix="1", full_name="Ana Souza", title="t", nicknames=("A", "B")
    )
    assert pick_vip_nickname(with_nicks, "seed") in {"A", "B"}
    assert pick_vip_nickname(with_nicks, "seed") == pick_vip_nickname(with_nicks, "seed")


def test_vip_builders_still_compose_with_arguments():
    from app.vip_profiles import (
        VipProfile,
        build_vip_balance_reply,
        build_vip_coupon_reply,
        build_vip_general_reply,
        build_vip_openai_context,
    )

    profile = VipProfile(
        phone_suffix="1", full_name="Ana Souza", title="Cliente VIP", nicknames=("A",)
    )
    balance = build_vip_balance_reply(profile, "A", "R$ 50,00", extra="Extra aqui.")
    assert "R$ 50,00" in balance
    assert "Extra aqui." in balance
    assert "CODE1" in build_vip_coupon_reply(profile, "A", "CODE1", "R$ 50,00")
    assert "base" in build_vip_general_reply(profile, "A", "base")
    assert "Ana Souza" in build_vip_openai_context(profile, "A")
