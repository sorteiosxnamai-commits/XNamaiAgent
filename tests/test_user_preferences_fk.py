from app.user_preferences import save_preferred_name


def test_save_preferred_name_soft_fails_on_fk_violation(monkeypatch):
    import app.user_preferences as prefs

    class BoomCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            raise RuntimeError("ForeignKeyViolation: users")

    class BoomConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return BoomCursor()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(prefs, "get_conn", lambda: BoomConn())
    # Must not raise — webhook path should continue without preferences.
    save_preferred_name(9, "Joao")
