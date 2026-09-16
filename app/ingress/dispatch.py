"""Drain both durable queues without letting one unavailable queue starve the other."""
from app.observability import log_exception


async def process_pending_queues(*, limit: int = 5) -> dict:
    from app.ingress.worker import process_inbox_batch
    from app.ingress.outbox_worker import process_outbox_batch
    results = {}
    for name, process in (("inbox", process_inbox_batch), ("outbox", process_outbox_batch)):
        try:
            results[name] = await process(limit=limit)
        except Exception as exc:
            log_exception("queue.dispatch_failed", exc, {"queue": name})
            results[name] = {"ok": False, "error": type(exc).__name__}
    return {"ok": all(result.get("ok") for result in results.values()), **results}
