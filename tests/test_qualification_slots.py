"""Qualification Slots: "o que sabemos?" — estados, precedencia e correcoes."""

from __future__ import annotations

import pytest

from app.models import SalesInterpretation
from app.sales.qualification_slots import (
    SLOT_NAMES,
    QualificationState,
    SlotSource,
    SlotStatus,
    build_qualification_state,
    explicitly_negates,
    is_explicit_correction,
    known_preferences,
    slots_from_interpretation,
    subject_identifiable,
)

K, U, R, NA = SlotStatus.KNOWN, SlotStatus.UNKNOWN, SlotStatus.REFUSED, SlotStatus.NOT_APPLICABLE


def _interp(**kw) -> SalesInterpretation:
    base = dict(domain="commerce", references_previous_context=False, needs_clarification=False, confidence=0.9)
    base.update(kw)
    return SalesInterpretation(**base)


def test_every_slot_starts_unknown_not_none():
    state = QualificationState()
    assert all(state.slot(name).status is U for name in SLOT_NAMES)
    assert build_qualification_state(_interp(goal="discover")).names_with(U) == list(SLOT_NAMES)


@pytest.mark.parametrize(
    ("fields", "slot", "value"),
    [
        (dict(subject={"product_type": "carregador"}), "category", "carregador"),
        (dict(subject={"brand": "MarcaB"}), "brand", "MarcaB"),
        (dict(subject={"model": "FKT-110C"}), "model", "FKT-110C"),
        (dict(subject={"reference": "T120.407"}), "product", "T120.407"),
        (dict(subject={"ean": "7891234567890"}), "product", "7891234567890"),
        (dict(preferences={"budget_max": 500}), "budget", {"min": None, "max": 500.0}),
        (dict(quantity=3), "quantity", 3),
        (dict(preferences={"attributes": ["USB-C", "20W"]}), "attributes", ["USB-C", "20W"]),
        (dict(preferences={"color": "preto"}), "color", "preto"),
        (dict(payment_method_preference="pix"), "payment_method", "pix"),
    ],
)
def test_explicit_value_is_known_with_its_source(fields, slot, value):
    state = build_qualification_state(_interp(goal="find", **fields), message_text="...")
    assert (state.slot(slot).status, state.slot(slot).value, state.slot(slot).source) == (
        K, value, SlotSource.USER_EXPLICIT)


def test_refused_only_with_explicit_evidence():
    declined = build_qualification_state(_interp(goal="recommend", preferences={"explicit_no_preferences": ["color"]}))
    assert declined.color.status is R
    silent = build_qualification_state(_interp(goal="recommend"))
    assert silent.color.status is U  # nao responder nao e recusar


def test_unknown_refused_and_not_applicable_are_distinct():
    refused = build_qualification_state(_interp(goal="recommend", preferences={"explicit_no_preferences": ["budget"]}))
    after_sale = build_qualification_state(_interp(goal="after_sales", references_previous_context=True))
    unknown = build_qualification_state(_interp(goal="recommend"))
    assert {refused.budget.status, after_sale.budget.status, unknown.budget.status} == {R, NA, U}


def test_not_applicable_outside_commerce_and_for_preferences_after_sale():
    assert set(build_qualification_state(_interp(domain="store_general")).names_with(NA)) == set(SLOT_NAMES)
    after = build_qualification_state(_interp(goal="after_sales", subject={"model": "FKT-110C"}))
    assert after.model.status is K and after.color.status is NA and after.budget.status is NA


def test_explicit_value_beats_refusal_in_the_same_turn():
    state = slots_from_interpretation(
        _interp(goal="find", subject={"brand": "MarcaB"}, preferences={"explicit_no_preferences": ["brand"]}))
    assert state.brand.status is K


# --- precedencia --------------------------------------------------------------


def test_current_budget_beats_remembered_budget():
    state = build_qualification_state(
        _interp(goal="recommend", preferences={"budget_max": 500}),
        message_text="quero até 500",
        memory_preferences={"budget_max": 1000},
    )
    assert (state.budget.value["max"], state.budget.source) == (500.0, SlotSource.USER_EXPLICIT)


def test_explicit_negation_drops_the_remembered_brand():
    state = build_qualification_state(
        _interp(goal="recommend", subject={"product_type": "fone"}),
        message_text="não quero MarcaB",
        memory_preferences={"brand": "MarcaB"},
    )
    assert state.brand.status is U and state.brand.value is None


def test_older_knowledge_fills_what_this_turn_did_not_say():
    state = build_qualification_state(
        _interp(goal="find", references_previous_context=True, subject={"product_type": "capa"}),
        message_text="e em azul?",
        state_preferences={"budget_max": 200, "color": "preto"},
        memory_preferences={"budget_max": 900, "style": "discreto"},
    )
    assert (state.budget.value["max"], state.budget.source) == (200, SlotSource.CONVERSATION_STATE)
    assert (state.style.value, state.style.source) == ("discreto", SlotSource.MEMORY)
    assert state.category.source is SlotSource.USER_INFERRED  # veio do contexto do turno


def test_conversation_state_beats_memory():
    state = build_qualification_state(
        _interp(goal="find"), state_preferences={"color": "azul"}, memory_preferences={"color": "preto"})
    assert (state.color.value, state.color.source) == ("azul", SlotSource.CONVERSATION_STATE)


def test_older_refusal_is_remembered_but_current_value_replaces_it():
    older = {"explicit_no_preferences": ["brand"]}
    assert build_qualification_state(_interp(goal="find"), state_preferences=older).brand.status is R
    now = build_qualification_state(_interp(goal="find", subject={"brand": "MarcaC"}), state_preferences=older)
    assert (now.brand.status, now.brand.value) == (K, "MarcaC")


# --- correcoes --------------------------------------------------------------------


@pytest.mark.parametrize("text", ["na verdade quero o preto", "não, eu quis dizer 20W", "mudei de ideia, azul",
                                  "me enganei, é USB-C"])
def test_correction_markers_are_detected(text):
    assert is_explicit_correction(text)


def test_customer_correction_replaces_the_previous_value_as_explicit():
    corrected = build_qualification_state(
        _interp(goal="find", references_previous_context=True, preferences={"color": "preto"}),
        message_text="na verdade quero o preto",
        state_preferences={"color": "azul"},
    )
    assert (corrected.color.value, corrected.color.source) == ("preto", SlotSource.USER_EXPLICIT)


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [("não quero MarcaB", "MarcaB", True), ("sem preto", "preto", True), ("quero MarcaB", "MarcaB", False),
     ("nada de rosa", "rosa", True), ("pode ser preto", "preto", False)],
)
def test_negation_detection(text, value, expected):
    assert explicitly_negates(text, value) is expected


# --- visoes consumidas pelo discovery (sem mudar DISCOVERY_STATE) -----------------


def test_known_preferences_keep_the_prompt_order():
    state = slots_from_interpretation(_interp(
        goal="recommend", subject={"brand": "MarcaB"},
        preferences={"attributes": ["x"], "recipient": "mae", "budget_max": 5, "color": "azul"}))
    assert list(known_preferences(state)) == ["budget", "color", "recipient", "brand", "attributes"]


@pytest.mark.parametrize(
    ("subject", "expected"),
    [({}, False), ({"product_type": "capa"}, True), ({"brand": "B"}, True), ({"model": "M"}, True),
     ({"reference": "R"}, True), ({"ean": "7891234567890"}, True)],
)
def test_subject_identifiable(subject, expected):
    assert subject_identifiable(slots_from_interpretation(_interp(goal="find", subject=subject))) is expected


def test_slots_never_decide_questions():
    """Descreve estado; perguntar e decisao futura do Scope Send Gate."""
    import app.sales.qualification_slots as module

    public = {name for name in dir(module) if not name.startswith("_")}
    assert not [name for name in public if "question" in name or "ask" in name or "next" in name]


def test_summary_is_pii_free():
    state = build_qualification_state(_interp(goal="recommend", preferences={"budget_max": 777}))
    assert "777" not in str(state.summary())
