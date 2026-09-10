"""Gate 5 — o runtime não pode mais depender de settings.tray_adapter_*."""

from __future__ import annotations

import inspect
import pathlib


def _source(relative: str) -> str:
    return pathlib.Path(relative).read_text(encoding="utf-8")


def test_openai_agent_does_not_read_tray_settings():
    source = _source("app/openai_agent.py")
    assert "tray_adapter_url" not in source
    assert "tray_adapter_token" not in source
    assert "tray_tools_enabled" not in source


def test_api_index_does_not_read_tray_settings():
    source = _source("api/index.py")
    assert "tray_adapter_url" not in source
    assert "tray_adapter_token" not in source
    assert "tray_tools_enabled" not in source
    assert "tray_adapter_configured" not in source


def test_tool_loop_condition_is_vendor_neutral():
    """As tools neutras precisam chegar ao modelo em intenção comercial,
    sem depender de nenhuma env de fornecedor."""
    from app import openai_agent

    source = inspect.getsource(openai_agent.generate_openai_reply_async)
    assert "TOOL_SCHEMAS" in source
    # nenhuma env de fornecedor pode condicionar o envio das tools
    assert "settings.tray_" not in source
    assert "tray_adapter_url" not in source
    assert "tray_adapter_token" not in source


def test_health_payload_has_no_tray_keys():
    import api.index as index

    source = inspect.getsource(index)
    assert '"tray_tools_enabled"' not in source
    assert '"tray_adapter_configured"' not in source
