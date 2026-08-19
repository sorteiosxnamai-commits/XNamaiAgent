"""Retention cleanup for private Instagram Story media objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import get_settings
from .db import ensure_tables, get_conn
from .instagram_story_media import SupabasePrivateStoryMediaStorage
from .observability import log_event


@dataclass
class CleanupResult:
    scanned: int = 0
    deleted_storage: int = 0
    cleared_paths: int = 0
    failed: int = 0
    skipped_confirmed: int = 0
    errors: list[str] = field(default_factory=list)


async def cleanup_expired_story_media(
    *,
    tenant_id: str | None = None,
    limit: int = 200,
) -> CleanupResult:
    """Delete expired private media blobs; keep hash + product association.

    Idempotent: rows with media_deleted_at set are skipped.
    Confirmed associations keep catalog linkage; only storage path is cleared.
    """
    settings = get_settings()
    retention_days = int(
        getattr(settings, "instagram_story_media_retention_days", 7) or 7
    )
    batch = max(1, min(500, int(limit)))
    result = CleanupResult()
    ensure_tables()
    storage = SupabasePrivateStoryMediaStorage()
    now = datetime.now(timezone.utc)

    clauses = [
        "media_storage_path IS NOT NULL",
        "media_deleted_at IS NULL",
        "(media_expires_at IS NOT NULL AND media_expires_at <= %s)",
    ]
    params: list[Any] = [now]
    # Also catch rows without media_expires_at based on created_at + retention.
    # Use OR via wrapping.
    sql_where = """
        media_storage_path IS NOT NULL
        AND media_deleted_at IS NULL
        AND (
            (media_expires_at IS NOT NULL AND media_expires_at <= %s)
            OR (
                media_expires_at IS NULL
                AND created_at <= %s
            )
        )
    """
    cutoff = now - timedelta(days=retention_days)
    params = [now, cutoff]
    if tenant_id:
        sql_where += " AND tenant_id = %s"
        params.append(str(tenant_id))
    params.append(batch)

    rows: list[dict[str, Any]] = []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, tenant_id, media_storage_path, match_status, media_sha256
                    FROM public.instagram_story_products
                    WHERE {sql_where}
                    ORDER BY created_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    tuple(params),
                )
                rows = [dict(r) for r in (cur.fetchall() or [])]
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        result.errors.append(type(exc).__name__)
        log_event(
            "story_media_retention_failed",
            {"code": type(exc).__name__},
        )
        return result

    result.scanned = len(rows)
    for row in rows:
        path = str(row.get("media_storage_path") or "")
        if not path:
            continue
        try:
            deleted = await storage.delete(storage_path=path)
            if not deleted and path.startswith("supabase://"):
                result.failed += 1
                result.errors.append("storage_delete_failed")
                continue
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.instagram_story_products
                        SET media_storage_path = NULL,
                            media_deleted_at = now(),
                            updated_at = now()
                        WHERE id = %s
                          AND tenant_id = %s
                          AND media_deleted_at IS NULL
                        """,
                        (int(row["id"]), str(row["tenant_id"])),
                    )
                conn.commit()
            result.deleted_storage += 1
            result.cleared_paths += 1
            if row.get("match_status") in {"matched", "manually_confirmed"}:
                result.skipped_confirmed += 0  # association preserved by design
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.errors.append(type(exc).__name__)

    log_event(
        "story_media_retention",
        {
            "scanned": result.scanned,
            "deleted_storage": result.deleted_storage,
            "failed": result.failed,
            "tenant_scoped": bool(tenant_id),
        },
    )
    return result
