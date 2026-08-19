from types import SimpleNamespace

from app.conversation_summary_policy import format_conversation_summary_block
from app.models import IncomingMessage
import app.prompt_compiler as compiler


def test_summary_injected_when_flag_enabled(monkeypatch):
    monkeypatch.setattr(
        compiler,
        "get_settings",
        lambda: SimpleNamespace(
            agent_db_persona_enabled=False,
            agent_contact_memory_in_prompt_enabled=False,
            agent_memory_auto_apply_enabled=False,
            agent_conversation_summary_in_prompt_enabled=True,
            agent_persona_tenant_id="newstore",
            agent_max_active_contact_memories=20,
            agent_max_contact_memory_chars=3000,
        ),
    )

    def fake_get(*, tenant_id, conversation_key):
        assert tenant_id == "newstore"
        assert conversation_key == "conv-1"
        return {
            "current_goal": "buscar Tissot",
            "summary": "goal=buscar Tissot",
            "open_questions": ["cor?"],
            "resolved_points": ["marca"],
            "user_corrections": [],
            "commitments": [],
        }

    monkeypatch.setattr(
        "app.conversation_summary_repository.get_conversation_summary",
        fake_get,
    )
    out = compiler.resolve_system_instructions(
        fallback_instructions="fallback contract",
        incoming=IncomingMessage(
            channel="whatsapp",
            text="oi",
            conversation_id="conv-1",
            sender_key="whatsapp:1",
        ),
    )
    assert "fallback contract" in out
    assert "<conversation_summary>" in out
    assert "NÃO use como fonte de preço" in out
    assert "buscar Tissot" in out


def test_summary_not_injected_when_flag_off(monkeypatch):
    monkeypatch.setattr(
        compiler,
        "get_settings",
        lambda: SimpleNamespace(
            agent_db_persona_enabled=False,
            agent_contact_memory_in_prompt_enabled=False,
            agent_memory_auto_apply_enabled=False,
            agent_conversation_summary_in_prompt_enabled=False,
            agent_persona_tenant_id="newstore",
        ),
    )

    def boom(**_kwargs):
        raise AssertionError("summary repository must not be called")

    monkeypatch.setattr(
        "app.conversation_summary_repository.get_conversation_summary",
        boom,
    )
    out = compiler.resolve_system_instructions(
        fallback_instructions="fallback contract",
        incoming=IncomingMessage(
            channel="whatsapp",
            text="oi",
            conversation_id="conv-1",
        ),
    )
    assert out == "fallback contract"
    assert format_conversation_summary_block(None).startswith("<conversation_summary>")
