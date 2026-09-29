"""Execucao da outbox: um consumidor, agendado com frequencia compativel com a janela."""

from __future__ import annotations

import ast
import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "outbox-retry.yml"


def test_outbox_is_drained_every_five_minutes_by_the_cron_endpoint():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'cron: "*/5 * * * *"' in text
    assert "/api/cron/process-outbox" in text
    assert "Authorization: Bearer ${CRON_SECRET}" in text
    assert "group: outbox-retry" in text  # runs never stack


def test_vercel_crons_stay_daily_and_do_not_duplicate_the_outbox_consumer():
    """Cron sub-diario na Vercel depende do plano; o agendamento frequente e o workflow."""
    crons = json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))["crons"]
    for cron in crons:
        minute, hour = cron["schedule"].split()[:2]
        assert "*" not in minute and "*" not in hour and "/" not in hour, cron
    assert "/api/cron/process-outbox" not in {cron["path"] for cron in crons}


def test_there_is_a_single_outbox_consumer():
    """Todos os gatilhos chamam process_outbox_batch; nenhum loop paralelo."""
    callers = set()
    for base in ("app", "api", "scripts"):
        for path in (REPO_ROOT / base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                    if name == "claim_pending_outbox":
                        callers.add(path.relative_to(REPO_ROOT).as_posix())
    assert callers == {"app/ingress/outbox_worker.py"}


def _simulated_attempts(window: int, poll: int, *, base: int = 30, cap: int = 300, max_attempts: int = 8) -> int:
    """1a tentativa inline em t=0; as demais so quando o consumidor roda (a cada ``poll``)."""
    from app.ingress.delivery_policy import OutboxRetryPolicy

    policy = OutboxRetryPolicy(base_seconds=base, max_seconds=cap, window_seconds=window)
    attempts, now = 1, 0
    while attempts < max_attempts:
        eligible = now + policy.backoff_seconds(attempts)
        now = -(-eligible // poll) * poll  # next scheduler tick
        if now >= window:
            break
        attempts += 1
    return attempts


def test_recommended_window_gives_real_retries_with_a_five_minute_scheduler():
    assert _simulated_attempts(900, 300) == 3
    assert _simulated_attempts(1800, 300) >= 5
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "AGENT_OUTBOX_RETRY_WINDOW_SECONDS=1800" in example
