"""Paridade da guarda de privacidade com o baseline 201bd16 (Opcao C).

Contexto: no baseline, ``_third_party_guardrail`` so era avaliada quando o
intent primario pertencia ao dominio pessoal (``balance``/``coupon_code``/
``raffle_history``/``simulation``). A Task H removeu esses intents do runtime,
o que deixaria a guarda MORTA. A primeira tentativa de correcao disparou a
guarda para qualquer mensagem, o que a deixou MAIS ESTRITA que o baseline:
mensagem comercial contendo telefone de terceiro passava a ser recusada.

Opcao C: um sinal interno de escopo pessoal, usado EXCLUSIVAMENTE pela guarda de
privacidade — sem handler, sem rota, sem capability, sem repositorio, sem banco.

Os 7 casos abaixo sao exatamente os da auditoria, com o resultado medido no
baseline 201bd16.
"""

from __future__ import annotations

import asyncio

import pytest

from app import openai_agent
from app.models import IncomingMessage

SENDER = "5511999999999"


class _Settings:
    openai_api_key = ""
    openai_model = "gpt-4.1-mini"
    max_reply_chars = 900
    agent_persona_runtime_enabled = False
    agent_history_limit = 0


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(openai_agent, "get_settings", lambda: _Settings())


def _intent(text: str) -> str:
    result = asyncio.run(
        openai_agent.generate_agent_reply_async(
            IncomingMessage(sender_phone=SENDER, text=text), {}
        )
    )
    return result.intent


# (texto, intent esperado, nota)
BASELINE_CASES = [
    ("saldo do Joao", "security_refusal", "conta de terceiro -> recusa"),
    ("qual o saldo do telefone 48988887777", "security_refusal", "conta de terceiro -> recusa"),
    ("quero ver o pedido do telefone 48988887777", "commerce", "comercio com telefone NAO e recusa"),
    ("voces tem Tissot Seastar para o 48988887777?", "commerce", "comercio com telefone NAO e recusa"),
    ("ola", "general", "saudacao"),
    ("quanto custa o relogio?", "commerce", "comercio"),
]


@pytest.mark.parametrize("text,expected,note", BASELINE_CASES, ids=[c[0][:34] for c in BASELINE_CASES])
def test_matches_baseline_201bd16(text, expected, note):
    assert _intent(text) == expected, note


def test_own_account_question_is_never_a_third_party_refusal():
    """"qual o meu saldo" nao pode virar recusa de terceiro.

    O baseline devolvia ``balance_inquiry`` — intent produzido pela feature de
    saldo, que saiu do runtime com a Task H. A paridade exigivel aqui e de
    SEGURANCA, nao de rotulo: a pergunta sobre a propria conta nunca pode ser
    tratada como consulta a dados de outra pessoa.
    """
    assert _intent("qual o meu saldo") != "security_refusal"


# --- o sinal pessoal existe, e existe SOMENTE para a guarda ----------------


def test_personal_account_scope_signal_exists():
    from app.privacy_scope import is_personal_account_scope

    assert is_personal_account_scope("qual o meu saldo") is True
    assert is_personal_account_scope("saldo do Joao") is True
    assert is_personal_account_scope("meu cupom") is True
    assert is_personal_account_scope("quero simular o uso do cartao presente") is True
    assert is_personal_account_scope("quanto custa o relogio?") is False
    assert is_personal_account_scope("ola") is False
    assert is_personal_account_scope("") is False
    assert is_personal_account_scope(None) is False


def test_privacy_scope_is_imported_only_by_the_guard():
    """Estrutural: nao pode existir caminho sinal-pessoal -> feature -> NewStore."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    importers = []
    for base in ("app", "api", "scripts"):
        for path in sorted((root / base).rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "privacy_scope.py":
                continue
            if "privacy_scope" in path.read_text(encoding="utf-8"):
                importers.append(path.relative_to(root).as_posix())
    assert importers == ["app/openai_agent.py"], (
        f"o sinal de escopo pessoal so pode ser consumido pela guarda: {importers}"
    )


def test_privacy_scope_imports_nothing_at_all():
    """O sinal e texto puro: sem repositorio, banco, tools, rede ou feature.

    Checado por AST (imports reais), nao por substring — substring daria falso
    positivo em qualquer palavra que contivesse o token.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "app" / "privacy_scope.py").read_text(
        encoding="utf-8"
    )
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            imported.update(a.name for a in node.names)
    assert imported <= {"__future__", "annotations"}, (
        f"privacy_scope precisa ser texto puro; importa: {sorted(imported)}"
    )


# --- nenhuma feature removida voltou ----------------------------------------


@pytest.mark.parametrize("symbol", [
    "build_balance_reply", "build_coupon_code_reply", "build_simulation_reply",
    "build_raffle_history_reply", "build_current_raffle_reply",
    "build_available_numbers_reply", "build_rules_reply_result",
    "_local_raffle_reply",
])
def test_no_feature_handler_was_reactivated(symbol):
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    # app/site_knowledge.py e persona protegida: guarda um build_simulation_reply
    # DORMENTE (sem chamador de runtime). Residuo de persona, nao handler.
    persona_protected = {"app/site_knowledge.py", "app/simulation.py"}
    offenders = [
        rel
        for base in ("app", "api", "scripts")
        for path in sorted((root / base).rglob("*.py"))
        if "__pycache__" not in path.parts
        and (rel := path.relative_to(root).as_posix()) not in persona_protected
        and symbol in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"{symbol} reativado em {offenders}"


@pytest.mark.parametrize("token", [
    "find_coupon_balance_by_phone", "find_user_coupon_code", "find_current_raffle",
    "find_open_draw", "find_user_raffle_participation", "find_last_payment_participation",
    "get_sorteio_conn", "SORTEIO_DATABASE_URL", "tray_tools", "tray_adapter_client",
    "TRAY_ADAPTER_URL", "mercadopago", "MP_ACCESS_TOKEN",
])
def test_no_removed_datasource_or_integration_reappeared(token):
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = [
        path.relative_to(root).as_posix()
        for base in ("app", "api", "scripts")
        for path in sorted((root / base).rglob("*.py"))
        if "__pycache__" not in path.parts and token in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"{token} reapareceu em {offenders}"


def test_no_executable_raffle_route_reappeared():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    # ``"local_raffle"`` ainda aparece em quality_judge._LOW_RISK_SOURCES como
    # entrada MORTA de uma lista de aceitacao — nenhum caminho o produz. O que
    # importa e que nada ATRIBUA esse response_source.
    for needle in (
        'scope_domain == "raffle"',
        '"domain": "raffle"',
        'response_source="local_raffle"',
    ):
        offenders = [
            path.relative_to(root).as_posix()
            for base in ("app", "api", "scripts")
            for path in sorted((root / base).rglob("*.py"))
            if "__pycache__" not in path.parts and needle in path.read_text(encoding="utf-8")
        ]
        assert not offenders, f"rota de sorteio reapareceu ({needle}): {offenders}"
