"""Unit tests for attendance learning classifiers and Etapa 9 promote gates."""

from types import SimpleNamespace

import pytest

from app.attendance_learning import (
    classify_attendance,
    promote_insights_to_extensions,
)


def test_classify_preference_misread_empty_catalog():
    row = {
        "customer_text": "feminino até 3000 reais",
        "agent_reply": "Não encontrei esse produto no catálogo agora.",
        "handoff_required": False,
        "intent": "commerce",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert result["outcome"] == "empty_catalog"
    assert "preference_misread" in result["failure_codes"]


def test_classify_trade_in_policy_miss():
    row = {
        "customer_text": "vcs estão comprando Certina seminovo?",
        "agent_reply": "Não compramos relógios seminovos, apenas vendemos produtos novos.",
        "handoff_required": False,
        "intent": "commerce",
        "response_metadata": {},
    }
    result = classify_attendance(row)
    assert result["outcome"] == "policy_miss"
    assert "trade_in_policy_miss" in result["failure_codes"]


def test_promote_creates_pending_extension_without_auto_activate(monkeypatch):
    calls = {"approve": 0, "create": 0, "updates": []}

    monkeypatch.setattr(
        "app.attendance_learning.get_settings",
        lambda: SimpleNamespace(agent_learning_auto_activate=False),
    )

    def fake_create(**kwargs):
        calls["create"] += 1
        assert kwargs["extension_key"].startswith("learning:")
        return {"id": 42, "status": "pending_review"}

    def fake_approve(*_a, **_k):
        calls["approve"] += 1
        raise AssertionError("approve_extension must not run when auto_activate=false")

    class FakeCursor:
        def execute(self, sql, params=None):
            calls["updates"].append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "app.attendance_learning.create_extension_proposal",
        fake_create,
    )
    monkeypatch.setattr(
        "app.instruction_extension_repository.approve_extension",
        fake_approve,
    )
    monkeypatch.setattr(
        "app.attendance_learning.get_conn",
        lambda: FakeConn(),
    )

    ext_id = promote_insights_to_extensions(
        tenant_id="newstore",
        insight_id=7,
        category="policy",
        insight_text="Não recusar trade-in; encaminhar humano.",
        confidence=0.8,
        importance=0.7,
    )
    assert ext_id == 42
    assert calls["create"] == 1
    assert calls["approve"] == 0
    assert len(calls["updates"]) == 1
    sql, params = calls["updates"][0]
    assert "status = 'applied'" not in sql
    assert "pending_review" in sql
    assert params == (42, 7)


def test_promote_never_auto_approves_even_when_flag_true(monkeypatch):
    """Cron must not call approve_extension — admin path only."""
    calls = {"approve": 0}

    monkeypatch.setattr(
        "app.attendance_learning.get_settings",
        lambda: SimpleNamespace(agent_learning_auto_activate=True),
    )
    monkeypatch.setattr(
        "app.attendance_learning.create_extension_proposal",
        lambda **_k: {"id": 99, "status": "pending_review"},
    )

    def fake_approve(extension_id, *, tenant_id, approved_by):
        calls["approve"] += 1

    class FakeCursor:
        def execute(self, sql, params=None):
            # stays pending_review
            assert "pending_review" in sql or "status" in sql

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "app.instruction_extension_repository.approve_extension",
        fake_approve,
    )
    monkeypatch.setattr("app.attendance_learning.get_conn", lambda: FakeConn())

    assert (
        promote_insights_to_extensions(
            tenant_id="newstore",
            insight_id=1,
            category="greeting",
            insight_text="Evitar saudação duplicada.",
            confidence=0.9,
            importance=0.8,
        )
        == 99
    )
    assert calls["approve"] == 0


@pytest.mark.asyncio
async def test_batch_default_does_not_promote(monkeypatch):
    monkeypatch.setattr(
        "app.attendance_learning.get_settings",
        lambda: SimpleNamespace(
            agent_persona_tenant_id="newstore",
            agent_learning_lookback_hours=2,
            agent_learning_batch_limit=120,
            agent_learning_auto_promote=False,
            agent_learning_auto_activate=False,
        ),
    )
    monkeypatch.setattr(
        "app.attendance_learning.fetch_recent_attendances",
        lambda **_k: [],
    )

    def boom(**_k):
        raise AssertionError("promote must not run when auto_promote is false")

    monkeypatch.setattr(
        "app.attendance_learning.promote_insights_to_extensions",
        boom,
    )
    from app.attendance_learning import run_attendance_learning_batch

    summary = await run_attendance_learning_batch()
    assert summary["extensions_promoted"] == 0
    assert summary["insights_upserted"] == 0
