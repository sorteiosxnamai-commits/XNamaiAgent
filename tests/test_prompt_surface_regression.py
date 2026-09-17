"""Snapshot dos prompts após a atualização autorizada da identidade Xnamai.

Mudanças de comportamento exigem revisão e atualização deliberada do fixture.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from prompt_surface import render_prompt_surface, serialize

BASELINE_FILE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "prompt_surface_baseline_xnamai.txt"
)

def _load_baseline() -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines(keepends=True):
        stripped = line.rstrip(chr(10))
        if stripped.startswith("===== ") and stripped.endswith(" ====="):
            current = stripped[6:-6]
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {key: "".join(value)[:-1] for key, value in blocks.items()}


def _expected_from_baseline(baseline: dict[str, str]) -> dict[str, str]:
    """A baseline JA e o estado aprovado: nenhuma transformacao pendente.

    Enquanto existia a neutralizacao comercial, duas alteracoes de prompt eram
    autorizadas e aplicadas aqui. Elas foram absorvidas pela baseline XNamai, e
    a lista voltou a ser vazia — que e o estado saudavel: qualquer diferenca
    agora e regressao ate que alguem decida o contrario.
    """
    return dict(baseline)


BASELINE = _load_baseline()
EXPECTED = _expected_from_baseline(BASELINE)


def test_baseline_fixture_is_not_empty():
    """Sem isto a guarda inteira pode passar a vacuo."""
    assert len(BASELINE) >= 20, f"fixture coletou apenas {len(BASELINE)} blocos"
    # O system prompt encolheu na migracao de identidade — saiu o conhecimento
    # institucional da marca legada. O piso aqui so garante que a fixture nao
    # esta vazia; nao e alvo de tamanho.
    assert len(BASELINE["openai.SYSTEM_INSTRUCTIONS"]) > 800
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
