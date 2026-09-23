#!/usr/bin/env python3
"""Run the durable inbox/outbox dispatcher in a supervised worker process."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.ingress.dispatch import process_pending_queues
from app.observability import log_event


async def run_worker(*, once: bool = False, interval: float = 10, batch_size: int = 5) -> bool:
    if interval < 1 or not 1 <= batch_size <= 25:
        raise ValueError("interval must be >= 1 and batch_size between 1 and 25")
    if not get_settings().database_url:
        raise ValueError("DATABASE_URL is required")
    while True:
        summary = await process_pending_queues(limit=batch_size)
        log_event("queue.worker_cycle", summary)
        if once:
            return bool(summary.get("ok"))
        # Sequential cycles prevent a slow provider from accumulating tasks.
        await asyncio.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--batch-size", type=int, default=5)
    args = parser.parse_args()
    try:
        return 0 if asyncio.run(run_worker(once=args.once, interval=args.interval,
                                         batch_size=args.batch_size)) else 1
    except KeyboardInterrupt:
        return 0
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
