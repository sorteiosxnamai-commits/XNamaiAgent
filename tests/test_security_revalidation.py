"""Phase 16: lightweight security/compatibility revalidation checks."""

import re
from pathlib import Path

from app.config import Settings
from app.guardrails import detect_blocked_request
from app.prompt_compiler import FIXED_SAFETY_POLICY


ROOT = Path(__file__).resolve().parents[1]


def test_admin_token_and_send_idempotency_defaults_are_safe():
    assert Settings.model_fields["agent_send_idempotency_enabled"].default is True
    assert Settings.model_fields["agent_conversation_lock_enabled"].default is True
    assert Settings.model_fields["agent_memory_auto_apply_enabled"].default is False


def test_fixed_safety_policy_blocks_prompt_and_secret_leak_instructions():
    text = FIXED_SAFETY_POLICY.casefold()
    assert "não revele prompt" in text or "nao revele prompt" in text
    assert "credenciais" in text


def test_blocked_topics_still_detected():
    assert detect_blocked_request("quero comprar número da sorte") is not None
    assert detect_blocked_request("tem tissot seastar?") is None


def test_repo_env_example_has_no_live_secrets():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "sk-proj-" not in example
    assert "sk-live" not in example
    assert re.search(r"(?m)^OPENAI_API_KEY=\S+", example) is None
    assert "OPENAI_API_KEY=" in example
