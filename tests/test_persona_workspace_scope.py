from types import SimpleNamespace

from app import prompt_compiler
from app.models import IncomingMessage


WORKSPACE = "aa774d20-509f-4d54-865b-7a5de22b6d30"


def test_prompt_loads_persona_from_the_message_workspace(monkeypatch):
    captured = {}

    def active(tenant_id, persona_key, workspace_id):
        captured.update(
            tenant_id=tenant_id,
            persona_key=persona_key,
            workspace_id=workspace_id,
        )
        return SimpleNamespace(
            id=67,
            version=1,
            instructions="Você é a Mai, assistente comercial virtual oficial da XNamai.",
            metadata={},
        )

    monkeypatch.setattr(prompt_compiler, "get_active_persona", active)
    monkeypatch.setattr(
        prompt_compiler,
        "get_settings",
        lambda: SimpleNamespace(
            agent_db_persona_enabled=True,
            chatbo_workspace_id="fallback-workspace",
        ),
    )

    compiled = prompt_compiler.compile_agent_prompt(
        incoming=IncomingMessage(text="oi", workspace_id=WORKSPACE),
        audit=False,
    )

    assert captured["workspace_id"] == WORKSPACE
    assert compiled.persona_version_id == 67
    assert compiled.used_db_persona is True
