"""Delivery policy for the outbound outbox: retry, backoff, ambiguity, dead-letter.

Guarantees — stated honestly
----------------------------
None of the providers used here (YCloud, Meta Graph Send API, Brevo) offers an
idempotency key. YCloud's ``externalId`` is a correlation field only: it is
echoed in status webhooks but does NOT deduplicate. So the system cannot promise
exactly-once delivery. What it does promise:

* **definite failure** (the request provably never reached the provider, or
  the provider rejected it before accepting) -> retried with backoff, bounded
  by attempts and by the retry window: at-least-once for these;
* **ambiguous failure** (the request reached the provider and the confirmation
  was lost: read timeout, connection dropped mid-response, 500/504) -> NOT
  retried by default. The row is dead-lettered as ``delivery_unknown`` with the
  ``outbox:<id>`` correlation id for reconciliation: at-most-once for these.
  ``AGENT_OUTBOX_RETRY_UNKNOWN_DELIVERY=true`` switches to at-least-once and
  accepts possible duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Transport-level classification lives with the HTTP helpers so channel
# adapters can use it without depending on the queue layer.
from app.http_resilience import (  # noqa: F401 - re-exported for queue callers
    NOT_SENT_EXCEPTIONS,
    RETRYABLE_SEND_STATUS,
    UNKNOWN_SEND_STATUS,
    classify_send_exception,
    classify_send_status,
)

SendOutcome = Literal["retryable", "permanent", "unknown"]

DELIVERY_UNKNOWN_ERROR = "delivery_unknown"


@dataclass(frozen=True)
class OutboxRetryPolicy:
    base_seconds: int = 30
    max_seconds: int = 300
    window_seconds: int = 900
    lease_seconds: int = 180
    retry_unknown_delivery: bool = False

    @classmethod
    def from_settings(cls, settings: Any) -> "OutboxRetryPolicy":
        def _int(name: str, default: int) -> int:
            try:
                return int(getattr(settings, name, default) or default)
            except (TypeError, ValueError):
                return default

        return cls(
            base_seconds=max(1, _int("agent_queue_retry_base_seconds", 30)),
            max_seconds=max(1, _int("agent_queue_retry_max_seconds", 300)),
            window_seconds=max(60, _int("agent_outbox_retry_window_seconds", 900)),
            lease_seconds=max(15, _int("agent_outbox_lease_seconds", 180)),
            retry_unknown_delivery=bool(
                getattr(settings, "agent_outbox_retry_unknown_delivery", False)
            ),
        )

    def backoff_seconds(self, attempts_done: int) -> int:
        """Delay after the ``attempts_done``-th failure (mirrors the claim SQL)."""
        exponent = min(max(attempts_done - 1, 0), 10)
        return int(min(self.max_seconds, self.base_seconds * (2**exponent)))

    def attempt_schedule(self, max_attempts: int) -> list[int]:
        """Seconds since enqueue at which each attempt becomes eligible."""
        schedule = [0]
        for attempts_done in range(1, max(1, max_attempts)):
            schedule.append(schedule[-1] + self.backoff_seconds(attempts_done))
        return schedule

    def effective_max_attempts(self, max_attempts: int) -> int:
        """Attempts that actually fit inside the retry window."""
        return sum(1 for at in self.attempt_schedule(max_attempts) if at < self.window_seconds)


def delivery_unknown_info(reason: str, *, provider: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": provider,
        "error": f"{DELIVERY_UNKNOWN_ERROR}:{reason}"[:120],
        "delivery_unknown": True,
    }


def resolve_failure(
    send_info: dict[str, Any],
    *,
    attempts: int,
    max_attempts: int,
    policy: OutboxRetryPolicy,
) -> tuple[bool, str]:
    """(dead, error) for a failed send. Ambiguous sends stop unless opted in."""
    error = str(send_info.get("error") or "send_failed")
    if send_info.get("delivery_unknown") and not policy.retry_unknown_delivery:
        return True, error if error.startswith(DELIVERY_UNKNOWN_ERROR) else f"{DELIVERY_UNKNOWN_ERROR}:{error}"
    dead = bool(send_info.get("permanent")) or attempts >= max_attempts
    return dead, error


def outbox_correlation_id(outbox_id: Any) -> str | None:
    """Stable id sent to providers that echo it (YCloud ``externalId``)."""
    return f"outbox:{outbox_id}" if outbox_id not in (None, "") else None
