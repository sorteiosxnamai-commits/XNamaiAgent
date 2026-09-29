from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app import security


@pytest.mark.asyncio
async def test_chatbo_sync_accepts_dedicated_bearer(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(chatbo_internal_token="dedicated-token"),
    )

    await security.verify_chatbo_sync_token("Bearer dedicated-token")


@pytest.mark.asyncio
async def test_chatbo_sync_rejects_wrong_bearer(monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(chatbo_internal_token="dedicated-token"),
    )

    with pytest.raises(HTTPException) as error:
        await security.verify_chatbo_sync_token("Bearer wrong-token")

    assert error.value.status_code == 401
