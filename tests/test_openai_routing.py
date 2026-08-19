from __future__ import annotations

from types import SimpleNamespace

from app.openai_routing import bucket_for_key, select_api_route


def test_traffic_percent_splits_population(monkeypatch):
    monkeypatch.setattr(
        "app.openai_routing.get_settings",
        lambda: SimpleNamespace(
            openai_responses_traffic_percent=0.10,
            openai_canary_sticky_routing=True,
        ),
    )
    responses = 0
    total = 2000
    for idx in range(total):
        if select_api_route(routing_key=f"user-{idx}") == "responses":
            responses += 1
    rate = responses / total
    assert 0.05 <= rate <= 0.15


def test_bucket_range():
    for key in ("a", "b", "whatsapp:1", "instagram:99"):
        assert 0 <= bucket_for_key(key) < 10_000
