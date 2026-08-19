from types import SimpleNamespace

from app.config import Settings, get_settings, resolved_sorteio_database_url


def test_sorteio_database_url_field_exists():
    assert "sorteio_database_url" in Settings.model_fields
    assert Settings.model_fields["sorteio_database_url"].alias == "SORTEIO_DATABASE_URL"


def test_resolved_sorteio_url_prefers_dedicated(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://agent/db")
    monkeypatch.setenv("SORTEIO_DATABASE_URL", "postgresql://sorteio/db")
    try:
        settings = Settings()
    finally:
        get_settings.cache_clear()
    assert resolved_sorteio_database_url(settings) == "postgresql://sorteio/db"


def test_resolved_sorteio_url_falls_back_to_agent_db(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://shared/db")
    monkeypatch.delenv("SORTEIO_DATABASE_URL", raising=False)
    try:
        settings = Settings()
    finally:
        get_settings.cache_clear()
    assert resolved_sorteio_database_url(settings) == "postgresql://shared/db"


def test_repository_uses_sorteio_connection(monkeypatch):
    import app.repository as repo

    calls = {"n": 0}

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return FakeCursor()

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            return None

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    def fake_sorteio_conn():
        calls["n"] += 1
        return FakeConn()

    monkeypatch.setattr(repo, "_sorteio_db_ready", lambda: True)
    monkeypatch.setattr(repo, "get_sorteio_conn", fake_sorteio_conn)

    result = repo.find_draw_payments(1)
    assert calls["n"] >= 1
    assert result.get("found") is True or result.get("lookup_error") or result.get("items") == []
