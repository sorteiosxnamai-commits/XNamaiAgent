from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.persona_repository as repo
import app.prompt_compiler as compiler
from app.models import IncomingMessage
from tests.persona_fakes import InMemoryPersonaStore


def _enable_persona(monkeypatch, enabled: bool = True):
    monkeypatch.setattr(
        compiler,
        "get_settings",
        lambda: SimpleNamespace(
            agent_db_persona_enabled=enabled,
            agent_prompt_compilation_audit_enabled=True,
            agent_debug_store_compiled_prompt=False,
            agent_max_recent_turns=8,
            agent_legacy_prompt_compat_enabled=False,
            agent_contact_memory_in_prompt_enabled=False,
            agent_memory_auto_apply_enabled=False,
            openai_api_mode="chat_completions",
            agent_persona_tenant_id="xnamai",
            agent_persona_key="xnamai_commercial",
        ),
    )


def test_resolve_passthrough_when_flag_disabled(monkeypatch):
    _enable_persona(monkeypatch, enabled=False)
    fallback = "fallback instructions in code"
    assert (
        compiler.resolve_system_instructions(fallback_instructions=fallback)
        == fallback
    )


def test_compile_includes_active_persona_and_safety(monkeypatch):
    store = InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    created = repo.create_persona_version(
        instructions="PERSONA_ATIVA_XNamai\n",
        name="Xnamai",
    )
    repo.activate_persona_version(created.id)

    incoming = IncomingMessage(channel="instagram", text="oi", sender_key="ig:1")
    compiled = compiler.compile_agent_prompt(
        incoming=incoming,
        fallback_instructions="fallback",
        audit=True,
    )
    assert compiled.used_db_persona is True
    assert compiled.persona_version_id == created.id
    assert "PERSONA_ATIVA_XNamai" in compiled.instructions
    assert "<business_identity>" in compiled.instructions
    assert "Você representa exclusivamente a Xnamai" in compiled.instructions
    assert "prevalecem sobre persona, memória e histórico" in compiled.instructions
    assert "https://www.xnamai.com/" in compiled.instructions
    assert "https://xnamai.meuspedidos.com.br/" in compiled.instructions
    assert "<fixed_safety_policy>" in compiled.instructions
    assert "<channel_overlay>" in compiled.instructions
    assert "instagram" in compiled.instructions
    assert len(store.compilations) == 1


def test_compile_recomputes_each_call_without_openai_state(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    created = repo.create_persona_version(instructions="P1\n", name="Xnamai")
    repo.activate_persona_version(created.id)

    a = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="a"),
        fallback_instructions="fb",
        audit=False,
    )
    b = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="b"),
        fallback_instructions="fb",
        audit=False,
    )
    assert a.instructions_hash == b.instructions_hash
    assert a.input_items[-1]["content"] == "a"
    assert b.input_items[-1]["content"] == "b"


def test_main_contract_not_duplicated_when_compat_off(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    monkeypatch.setattr(
        compiler,
        "get_settings",
        lambda: SimpleNamespace(
            agent_db_persona_enabled=True,
            agent_prompt_compilation_audit_enabled=False,
            agent_debug_store_compiled_prompt=False,
            agent_max_recent_turns=8,
            agent_legacy_prompt_compat_enabled=False,
            openai_api_mode="chat_completions",
            agent_persona_tenant_id="xnamai",
            agent_persona_key="xnamai_commercial",
        ),
    )
    contract = "CONTRATO_UNICO_DE_SEGURANCA_E_TOM_XNamai_XYZ"
    redundant = f"<legacy_agent_contract>\n{contract}\n</legacy_agent_contract>"
    compiled = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="oi"),
        fallback_instructions=contract,
        extra_system_blocks=[redundant],
        audit=False,
    )
    assert compiler.count_contract_occurrences(compiled.instructions, contract) == 1
    assert "<legacy_agent_contract>" not in compiled.instructions


def test_legacy_compat_flag_reembeds_contract(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    monkeypatch.setattr(
        compiler,
        "get_settings",
        lambda: SimpleNamespace(
            agent_db_persona_enabled=True,
            agent_prompt_compilation_audit_enabled=False,
            agent_debug_store_compiled_prompt=False,
            agent_max_recent_turns=8,
            agent_legacy_prompt_compat_enabled=True,
            openai_api_mode="chat_completions",
            agent_persona_tenant_id="xnamai",
            agent_persona_key="xnamai_commercial",
        ),
    )
    contract = "CONTRATO_COMPAT_ABC"
    extras = compiler.legacy_contract_extra_blocks(
        contract, tag="legacy_agent_contract"
    )
    compiled = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="oi"),
        fallback_instructions=contract,
        extra_system_blocks=extras,
        audit=False,
    )
    assert "<legacy_agent_contract>" in compiled.instructions
    assert compiler.count_contract_occurrences(compiled.instructions, contract) >= 2


def test_current_message_not_duplicated_in_input_items(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    created = repo.create_persona_version(instructions="P\n", name="Xnamai")
    repo.activate_persona_version(created.id)

    compiled = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="mensagem atual"),
        fallback_instructions="fb",
        recent_turns=[
            {"role": "user", "content": "anterior"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "mensagem atual"},
        ],
        audit=False,
    )
    user_contents = [
        item["content"]
        for item in compiled.input_items
        if item.get("role") == "user"
    ]
    assert user_contents.count("mensagem atual") == 1


def test_db_persona_keeps_operational_contract_without_duplicating_persona(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    created = repo.create_persona_version(
        instructions="TOM_E_IDENTIDADE_XNamai\n",
        name="Xnamai",
    )
    repo.activate_persona_version(created.id)

    operational = "CONTRATO_OPERACIONAL_SALES_E_TOOLS_XYZ"
    compiled = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="oi"),
        fallback_instructions=operational,
        extra_system_blocks=[
            f"<sales_responder_contract>\n{operational}\n</sales_responder_contract>"
        ],
        audit=False,
    )
    assert compiled.used_db_persona is True
    assert "TOM_E_IDENTIDADE_XNamai" in compiled.instructions
    assert "<operational_contract>" in compiled.instructions
    assert operational in compiled.instructions
    assert compiler.count_contract_occurrences(compiled.instructions, operational) == 1
    assert "<sales_responder_contract>" not in compiled.instructions


def test_persona_missing_falls_back_to_code_contract(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    compiled = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="oi"),
        fallback_instructions="FALLBACK_CODE_CONTRACT",
        audit=False,
    )
    assert compiled.used_db_persona is False
    assert compiled.fallback_reason == "persona_active_missing"
    assert "FALLBACK_CODE_CONTRACT" in compiled.instructions
    assert "<operational_contract>" not in compiled.instructions


def test_legacy_store_persona_is_rejected_and_falls_back_to_xnamai_contract(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    stale_instructions = "Você vende reló" + "gios da New " + "Store.\n"
    created = repo.create_persona_version(
        instructions=stale_instructions,
        name="Cadastro antigo",
    )
    repo.activate_persona_version(created.id)

    compiled = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="oi"),
        fallback_instructions="CONTRATO_ATUAL_XNAMAI",
        audit=False,
    )

    assert compiled.used_db_persona is False
    assert compiled.persona_version_id is None
    assert compiled.fallback_reason == "legacy_persona_rejected"
    assert "CONTRATO_ATUAL_XNAMAI" in compiled.instructions
    assert stale_instructions.strip() not in compiled.instructions


def test_tenant_isolation(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    a = repo.create_persona_version(
        instructions="TENANT_A\n",
        tenant_id="tenant_a",
        name="A",
    )
    repo.activate_persona_version(a.id, tenant_id="tenant_a")
    b = repo.create_persona_version(
        instructions="TENANT_B\n",
        tenant_id="tenant_b",
        name="B",
    )
    repo.activate_persona_version(b.id, tenant_id="tenant_b")

    compiled_a = compiler.compile_agent_prompt(
        tenant_id="tenant_a",
        fallback_instructions="fb",
        audit=False,
    )
    compiled_b = compiler.compile_agent_prompt(
        tenant_id="tenant_b",
        fallback_instructions="fb",
        audit=False,
    )
    assert "TENANT_A" in compiled_a.instructions
    assert "TENANT_B" not in compiled_a.instructions
    assert "TENANT_B" in compiled_b.instructions
    assert "TENANT_A" not in compiled_b.instructions


@pytest.mark.parametrize(
    "legit",
    [
        "Temos smartwatch compatível com Android e iOS.",
        "Ajude o cliente a escolher um relógio inteligente para corrida.",
        "Pulseiras para Apple Watch e capas para celular fazem parte do catálogo.",
        "Relógios, watches, fones e carregadores podem aparecer no catálogo atual.",
        "A Xnamai não faz sorteios nem promoções por sorteio.",
        "Você é o assistente comercial oficial da XNamai, distribuidora de eletrônicos.",
    ],
)
def test_product_words_alone_do_not_reject_a_persona(legit):
    assert compiler._has_legacy_store_identity(legit) is False


@pytest.mark.parametrize(
    "stale",
    [
        "Você é o NewStoreAgent, atendente virtual.",
        "Você é o assistente comercial oficial da New Store.",
        "persona_key: newstore_commercial",
        "Consulte https://www.sorteio" + "newstore.com.br/",
        "Você é o NSAgent de atendimento.",
        "Explique o sorteio e o saldo do Cartão Presente.",
        "Informe o resultado do sorteio da Lotomania.",
    ],
)
def test_legacy_identity_signals_reject_a_persona(stale):
    assert compiler._has_legacy_store_identity(stale) is True


def test_smartwatch_persona_is_used_not_replaced_by_fallback(monkeypatch):
    InMemoryPersonaStore().install(monkeypatch)
    _enable_persona(monkeypatch, enabled=True)
    created = repo.create_persona_version(
        instructions="Persona Xnamai. Vendemos smartwatch, relógio inteligente e pulseiras de Apple Watch.\n",
        name="Catálogo com relógios",
    )
    repo.activate_persona_version(created.id)

    compiled = compiler.compile_agent_prompt(
        incoming=IncomingMessage(channel="whatsapp", text="oi"),
        fallback_instructions="CONTRATO_ATUAL_XNAMAI",
        audit=False,
    )

    assert compiled.used_db_persona is True
    assert compiled.fallback_reason is None
    assert "relógio inteligente" in compiled.instructions
