from app.user_preferences import (
    build_memory_context,
    detect_memory_note,
    detect_preferred_name_update,
    detect_speaking_style_update,
    extract_first_name,
    resolve_display_name,
)


def test_extract_first_name():
    assert extract_first_name("Tironi Silva") == "Tironi"
    assert extract_first_name("A") is None


def test_detect_preferred_name_update():
    assert detect_preferred_name_update("Pode me chamar de Tito") == "Tito"
    assert detect_preferred_name_update("Qual meu saldo?") is None


def test_detect_speaking_style_update():
    assert detect_speaking_style_update("Pode falar mais direto comigo") == "direto"
    assert detect_speaking_style_update("Quero respostas mais formais") == "formal"


def test_detect_memory_note():
    assert detect_memory_note("Lembra que prefiro respostas curtas") == "prefiro respostas curtas"
    assert detect_memory_note("saldo") is None


def test_build_memory_context_includes_preferences():
    context = build_memory_context(
        {
            "preferred_name": "Tironi",
            "speaking_style": "direto",
            "memory_notes": ["Prefere respostas curtas"],
            "recent_topics": ["saldo do cartão presente", "números disponíveis"],
        }
    )

    assert "Tironi" in context
    assert "direto" in context
    assert "Prefere respostas curtas" in context
    assert "números disponíveis" in context
    assert "Não pergunte de novo" in context


def test_resolve_display_name_prefers_saved_preference():
    assert resolve_display_name("Tironi Silva", {"preferred_name": "Tito"}) == "Tito"
    assert resolve_display_name("Tironi Silva", {"preferred_name": None}) == "Tironi"
