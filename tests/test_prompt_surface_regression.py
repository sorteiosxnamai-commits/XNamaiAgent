"""REGRA ZERO: nada pode alterar o texto que chega ao modelo.

Referencia comportamental: ``201bd16`` (commit anterior a Parte 1), capturado em
``tests/fixtures/prompt_surface_baseline_201bd16.txt`` pelo mesmo renderizador
usado aqui (``tests/prompt_surface.py``).

Por que renderizar em vez de comparar arquivos: na Parte 1 ``openai_agent.py``
ficou textualmente quase intacto enquanto o system prompt que ele monta encolhia
de 5471 para 2220 chars — o conteudo vinha de ``site_knowledge``. Um teste de
arquivo nao veria isso. Este ve.

A superficie coberta e a definicao COMPLETA de PROMPT SURFACE: instrucoes de
sistema e de venda, ``prompt_compiler``, ``prompt_layers``, critique/judge,
interpretacao de turno, politicas de memoria, greeting, conhecimento
institucional, catalogo de capacidades, instrucoes multimodais, copy
deterministica de canal e os schemas de tool. Um arquivo NAO e seguro so
porque nao contem a palavra "persona": se o resultado chega ao modelo, esta
aqui. Ao cobrir um bloco novo, regenere o fixture a partir de ``201bd16``.

DUAS — e apenas duas — alteracoes de prompt sao autorizadas nesta Parte 1:

1. a linha de politica ``"Usar APIs Tray para fatos comerciais; ..."`` vira
   ``"Usar a fonte comercial oficial para fatos comerciais; ..."`` (autorizada
   explicitamente pelo dono do produto);
2. as capacidades de sorteio somem do catalogo, porque o dominio de raffle saiu
   do runtime (Task H) — nao ha mais feature para anunciar ao modelo.

Qualquer outra divergencia e regressao de persona e reprova.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from prompt_surface import render_prompt_surface, serialize

BASELINE_FILE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "prompt_surface_baseline_201bd16.txt"
)

OLD_POLICY = "Usar APIs Tray para fatos comerciais; usar histórico/WORKING_MEMORY para continuidade"
NEW_POLICY = "Usar a fonte comercial oficial para fatos comerciais; usar histórico/WORKING_MEMORY para continuidade"


def _load_baseline() -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.startswith("===== ") and stripped.endswith(" ====="):
            current = stripped[6:-6]
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {key: "".join(value)[:-1] for key, value in blocks.items()}


def _expected_from_baseline(baseline: dict[str, str]) -> dict[str, str]:
    """Baseline + as duas transformacoes autorizadas. Nada mais."""
    expected = dict(baseline)

    # (1) linha de politica
    for key in ("capability_catalog.for_prompt", "capability_catalog.payload"):
        assert OLD_POLICY in expected[key], f"baseline sem a linha de politica em {key}"
        expected[key] = expected[key].replace(OLD_POLICY, NEW_POLICY)

    # (2) capacidades de sorteio fora do catalogo
    payload = json.loads(expected["capability_catalog.payload"])
    payload.pop("raffle_capabilities", None)
    payload["apis"] = [
        item for item in payload["apis"] if item.get("domain") != "raffle"
    ]
    expected["capability_catalog.payload"] = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    )
    expected["capability_catalog.for_prompt"] = "\n".join(
        line
        for line in expected["capability_catalog.for_prompt"].split("\n")
        if not line.startswith("Capacidades de sorteio: ")
    )
    return expected


BASELINE = _load_baseline()
EXPECTED = _expected_from_baseline(BASELINE)


def test_baseline_fixture_is_not_empty():
    """Sem isto a guarda inteira pode passar a vacuo."""
    assert len(BASELINE) >= 20, f"fixture coletou apenas {len(BASELINE)} blocos"
    assert len(BASELINE["openai.SYSTEM_INSTRUCTIONS"]) > 5000
    assert {"openai.SYSTEM_INSTRUCTIONS", "TOOL_SCHEMAS"} <= set(BASELINE)


@pytest.mark.parametrize("block", sorted(EXPECTED))
def test_prompt_block_matches_baseline(block):
    actual = render_prompt_surface()
    assert block in actual, f"bloco {block} sumiu da superficie de prompt"
    assert actual[block] == EXPECTED[block], (
        f"texto que chega ao modelo mudou em {block} "
        f"({len(EXPECTED[block])} -> {len(actual[block])} chars)"
    )


def test_no_unauthorized_prompt_diff_lines():
    """SYSTEM_PROMPT_UNAUTHORIZED_DIFF_LINES deve ser 0."""
    import difflib

    actual = render_prompt_surface()
    diff = [
        line
        for line in difflib.unified_diff(
            serialize(EXPECTED).splitlines(), serialize(actual).splitlines(), lineterm="", n=0
        )
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert diff == [], f"{len(diff)} linhas nao autorizadas:\n" + "\n".join(diff[:40])


def test_system_prompt_is_byte_identical_to_baseline():
    actual = render_prompt_surface()["openai.SYSTEM_INSTRUCTIONS"]
    assert actual == BASELINE["openai.SYSTEM_INSTRUCTIONS"]


def test_no_empty_interpolation_reaches_the_model():
    """Regressao do bug ``"...no WhatsApp ."`` (constante vazia interpolada).

    O sinal e o ESPACO antes da pontuacao: ``"WhatsApp ."`` e interpolacao
    vazia, enquanto ``"grupo oficial WhatsApp."`` e frase legitima.
    """
    import re

    for block, text in render_prompt_surface().items():
        for pattern in (
            r"WhatsApp[ 	]+[.,]",
            r"em[ 	]+[.,]",
            r"acesse[ 	]+[.,]",
            r"\([ 	]*\)",
        ):
            assert not re.search(pattern, text, flags=re.IGNORECASE), (
                f"interpolacao vazia em {block}: {pattern}"
            )
