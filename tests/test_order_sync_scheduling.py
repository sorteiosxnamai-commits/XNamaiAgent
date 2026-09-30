import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("path,method,env", [
    ("/api/admin/commerce/sync/orders", "post", "ADMIN_API_TOKEN"),
    ("/api/cron/commerce/sync/orders", "post", "CRON_SECRET"),
])
def test_order_sync_authenticates_and_uses_same_provider_runner(monkeypatch, path, method, env):
    from types import SimpleNamespace as NS
    from app.config import get_settings
    import api.index as index
    calls = []

    async def runner():
        calls.append(True)
        return {"ok": True, "incremental": {"complete": True}, "history": {"complete": False}}

    monkeypatch.setattr("app.commerce.provider.get_commerce_provider", lambda: NS(run_order_sync=runner))
    monkeypatch.setenv(env, "test-order-cron")
    get_settings.cache_clear()
    try:
        client = TestClient(index.app)
        assert client.post(path).status_code == 401
        assert client.post(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert calls == []
        response = client.post(path, headers={"Authorization": "Bearer test-order-cron"})
        assert response.status_code == 200 and response.json()["ok"]
        assert calls == [True]
    finally:
        get_settings.cache_clear()
