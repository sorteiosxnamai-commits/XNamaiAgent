import pytest

from app.pix_settlement import (
    brl_to_cents,
    settle_approved_pix_payment,
    validate_pix_settlement_amounts,
)


def test_brl_to_cents_exact():
    assert brl_to_cents("10.50") == 1050
    assert brl_to_cents(10.5) == 1050
    assert brl_to_cents("3399.99") == 339999


def test_validate_requires_approved_and_exact_match():
    assert (
        validate_pix_settlement_amounts(
            mp_status="pending",
            mp_amount_cents=1000,
            stored_amount_cents=1000,
            expected_amount_cents=1000,
        )
        == "pix_not_approved"
    )
    assert (
        validate_pix_settlement_amounts(
            mp_status="approved",
            mp_amount_cents=1000,
            stored_amount_cents=1000,
            expected_amount_cents=999,
        )
        == "amount_mismatch"
    )
    assert (
        validate_pix_settlement_amounts(
            mp_status="approved",
            mp_amount_cents=1000,
            stored_amount_cents=1000,
            expected_amount_cents=1000,
        )
        is None
    )


def _row(**overrides):
    base = {
        "mp_payment_id": "mp-1",
        "status": "approved",
        "amount_cents": 1050,
        "settlement_status": "pending",
        "tray_order_id": None,
        "checkout_snapshot": {
            "expected_amount_cents": 1050,
            "order_payload": {
                "session_id": "sess",
                "products": [{"product_id": 1, "price": "10.50", "quantity": 1}],
                "shipping": {"value": "0"},
                "payment": {"name": "Pix"},
                "customer": {"name": "A"},
                "address": {},
            },
        },
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_settle_rejects_amount_mismatch(monkeypatch):
    import app.pix_settlement as settlement

    monkeypatch.setattr(settlement.repo, "get_pix_payment_by_mp_id", lambda _pid: _row())
    monkeypatch.setattr(
        settlement.repo,
        "mark_pix_settlement",
        lambda *a, **k: {"settlement_status": "failed"},
    )

    result = await settle_approved_pix_payment(
        "mp-1",
        mp_payload={"id": "mp-1", "status": "approved", "transaction_amount": 9.99},
    )
    assert result["ok"] is False
    assert result["reason"] == "amount_mismatch"
    assert result["action"] == "rejected"


@pytest.mark.asyncio
async def test_settle_skips_when_not_approved(monkeypatch):
    import app.pix_settlement as settlement

    monkeypatch.setattr(settlement.repo, "get_pix_payment_by_mp_id", lambda _pid: _row())
    result = await settle_approved_pix_payment(
        "mp-1",
        mp_payload={"id": "mp-1", "status": "pending", "transaction_amount": 10.5},
    )
    assert result["ok"] is False
    assert result["reason"] == "pix_not_approved"
    assert result["action"] == "skipped"


@pytest.mark.asyncio
async def test_settle_creates_tray_order_when_amounts_match(monkeypatch):
    import app.pix_settlement as settlement

    monkeypatch.setattr(settlement.repo, "get_pix_payment_by_mp_id", lambda _pid: _row())
    monkeypatch.setattr(settlement.repo, "claim_pix_settlement", lambda _pid: _row(settlement_status="processing"))

    marked = {}

    def mark(pid, **kwargs):
        marked.update(kwargs)
        marked["mp_payment_id"] = pid
        return {"settlement_status": kwargs["settlement_status"], "tray_order_id": kwargs.get("tray_order_id")}

    monkeypatch.setattr(settlement.repo, "mark_pix_settlement", mark)

    async def create(payload):
        assert payload["session_id"] == "sess"
        return {"order_id": "tray-77", "success": True}

    result = await settle_approved_pix_payment(
        "mp-1",
        mp_payload={"id": "mp-1", "status": "approved", "transaction_amount": 10.5},
        create_order=create,
    )
    assert result["ok"] is True
    assert result["action"] == "settled"
    assert result["tray_order_id"] == "tray-77"
    assert result["reason"] == "created_after_pix_approval"
    assert marked["settlement_status"] == "completed"
    assert marked["tray_order_id"] == "tray-77"


@pytest.mark.asyncio
async def test_settle_idempotent_when_already_completed(monkeypatch):
    import app.pix_settlement as settlement

    monkeypatch.setattr(
        settlement.repo,
        "get_pix_payment_by_mp_id",
        lambda _pid: _row(settlement_status="completed", tray_order_id="tray-1"),
    )
    result = await settle_approved_pix_payment(
        "mp-1",
        mp_payload={"id": "mp-1", "status": "approved", "transaction_amount": 10.5},
    )
    assert result["action"] == "already_settled"
    assert result["tray_order_id"] == "tray-1"


@pytest.mark.asyncio
async def test_webhook_approved_runs_settle_with_match(monkeypatch):
    import app.pix_payment_service as service

    async def fake_refresh(payment_id, *, settings=None):
        return {
            "payment_id": payment_id,
            "status": "approved",
            "row": {"settlement_status": "pending"},
            "raw": {"id": payment_id, "status": "approved", "transaction_amount": 10.5},
        }

    async def fake_settle(payment_id, *, mp_payload=None, create_order=None):
        assert payment_id == "abc"
        assert mp_payload["status"] == "approved"
        return {
            "ok": True,
            "action": "settled",
            "reason": "created_after_pix_approval",
            "tray_order_id": "900",
            "settlement_status": "completed",
        }

    monkeypatch.setattr(service, "refresh_pix_payment_status", fake_refresh)
    monkeypatch.setattr(service, "settle_approved_pix_payment", fake_settle)

    from types import SimpleNamespace

    settings = SimpleNamespace(
        mp_access_token="t",
        mercadopago_access_token="",
        resolved_mp_access_token=lambda: "t",
    )
    result = await service.handle_mercadopago_webhook(
        {"type": "payment", "data": {"id": "abc"}},
        settings=settings,
    )
    assert result["settlement"]["action"] == "settled"
    assert result["settlement"]["tray_order_id"] == "900"
