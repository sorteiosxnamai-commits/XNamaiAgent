# NOTA (correcao da Parte 1): os testes que afirmavam "nao configurado" para
# URLs, contatos, tabela de credito e registro VIP foram removidos. Aquele
# comportamento era uma alteracao NAO AUTORIZADA de persona, revertida para o
# baseline 201bd16. O rebranding e tarefa separada.

"""Regras genéricas de perfil VIP — sem registro de marca configurado.

Os perfis legados (pessoas da marca anterior) foram removidos do runtime na
Parte 1. O que permanece e continua sendo testado é comportamento genérico e
reutilizável: normalização/casamento de telefone por sufixo, escolha
determinística de apelido e montagem das respostas personalizadas.
"""

from app.vip_profiles import (
    VIP_PROFILES,
    VipProfile,
    build_vip_balance_reply,
    build_vip_greeting,
    get_vip_profile,
    pick_vip_nickname,
)

_PROFILE = VipProfile(
    phone_suffix="21969544700",
    full_name="Cliente Exemplo",
    title="Diretor",
    nicknames=("Chefe", "Doutor"),
)


def test_get_vip_profile_handles_missing_phone_without_registry():
    assert get_vip_profile(None) is None
    assert get_vip_profile("") is None


def test_suffix_matching_rule_is_preserved(monkeypatch):
    """A regra de casamento por sufixo E.164 é genérica e continua valendo."""
    monkeypatch.setattr("app.vip_profiles.VIP_PROFILES", (_PROFILE,))

    assert get_vip_profile("21969544700") is _PROFILE
    assert get_vip_profile("+55 21 96954-4700") is _PROFILE
    assert get_vip_profile("85999498149") is None


def test_pick_vip_nickname_is_deterministic_and_from_the_known_set():
    nickname = pick_vip_nickname(_PROFILE, "saldo")
    assert nickname in _PROFILE.nicknames
    assert pick_vip_nickname(_PROFILE, "saldo") == nickname


def test_pick_vip_nickname_falls_back_to_first_name_without_nicknames():
    bare = VipProfile(
        phone_suffix="11999999999",
        full_name="Fulano de Tal",
        title="Cliente",
        nicknames=(),
    )
    assert pick_vip_nickname(bare) == "Fulano"


def test_build_vip_balance_reply_is_personalized():
    reply = build_vip_balance_reply(_PROFILE, "Chefe", "R$ 50,00")
    assert "Chefe" in reply
    assert "Cliente Exemplo" in reply
    assert "R$ 50,00" in reply
    assert build_vip_greeting(_PROFILE, "Chefe") in reply
