"""Characterization de ``_discovery_state`` ANTES da extracao dos Qualification Slots.

``DISCOVERY_STATE`` vai para o prompt de esclarecimento como JSON: a
comparacao e do TEXTO serializado (ordem de chaves inclusa), nao so do dict.
Fixture gravada do codigo existente: ``tests/fixtures/discovery_state_characterization.json``.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.models import SalesInterpretation

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "discovery_state_characterization.json"
EXPECTED = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _interp(**kw) -> SalesInterpretation:
    base = dict(domain="commerce", references_previous_context=False, needs_clarification=False, confidence=0.9)
    base.update(kw)
    return SalesInterpretation(**base)


_CLAR = {"role": "assistant", "content": "Qual o modelo?", "metadata": {"safety_reason": "commerce_clarification"}}
_USER = {"role": "user", "content": "x"}

CASES = {
    "vazio": (_interp(goal="discover"), []),
    "categoria": (_interp(goal="discover", subject={"product_type": "celular"}), []),
    "orcamento_marca": (_interp(goal="recommend", subject={"product_type": "fone", "brand": "MarcaB"},
                                preferences={"budget_max": 500}), []),
    "atributos_cor": (_interp(goal="find", subject={"product_type": "cabo"},
                              preferences={"color": "preto", "attributes": ["USB-C"]}), []),
    "sem_preferencia": (_interp(goal="recommend", subject={"product_type": "capa"},
                                preferences={"explicit_no_preferences": ["color", "brand", "color"]}), []),
    "orcamento_minmax": (_interp(goal="recommend", preferences={"budget_min": 100, "budget_max": 300}), []),
    "modelo_ref": (_interp(goal="inspect", subject={"model": "FKT-110C", "reference": "T120"}), []),
    "forcar_busca": (_interp(goal="discover", subject={"product_type": "capa"}, stop_clarification=True),
                     [_CLAR, _USER, _CLAR, _USER]),
    "perguntas_recentes": (_interp(goal="discover"), [_USER, _CLAR, _USER, _CLAR]),
    "ocasiao_destinatario": (_interp(goal="recommend", preferences={"occasion": "presente", "recipient": "mae",
                                                                    "style": "discreto", "material": "couro"}), []),
}


def test_fixture_covers_every_case():
    assert set(CASES) == set(EXPECTED)


@pytest.mark.parametrize("case", sorted(CASES))
def test_discovery_state_reaches_the_prompt_unchanged(case):
    from app.sales_agent import _discovery_state

    interpretation, turns = CASES[case]
    actual = _discovery_state(interpretation, turns)
    assert json.dumps(actual, ensure_ascii=False) == json.dumps(EXPECTED[case], ensure_ascii=False)
