"""Tenant-scoped Instagram Story ↔ product association repository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import get_settings
from .db import ensure_tables, get_conn, to_jsonb
from .instagram_story_models import StoryProductAssociation, StoryVisualUnderstanding


def _row_to_association(row: dict[str, Any] | None) -> StoryProductAssociation | None:
    if not row:
        return None
    return StoryProductAssociation(
        id=int(row["id"]) if row.get("id") is not None else None,
        tenant_id=str(row.get("tenant_id") or ""),
        provider=str(row.get("provider") or "brevo"),
        instagram_account_id=str(row.get("instagram_account_id") or ""),
        story_media_id=str(row.get("story_media_id") or ""),
        story_message_id=row.get("story_message_id"),
        story_permalink=row.get("story_permalink"),
        media_type=str(row.get("media_type") or "unknown"),
        source_timestamp=row.get("source_timestamp"),
        story_expires_at=row.get("story_expires_at"),
        media_storage_path=row.get("media_storage_path"),
        media_sha256=row.get("media_sha256"),
        thumbnail_sha256=row.get("thumbnail_sha256"),
        analysis_version=row.get("analysis_version"),
        catalog_item_key=row.get("catalog_item_key"),
        product_id=row.get("product_id"),
        variant_id=row.get("variant_id"),
        match_source=str(row.get("match_source") or "pending"),
        match_status=str(row.get("match_status") or "pending"),
        match_confidence=float(row.get("match_confidence") or 0.0),
        visual_analysis=dict(row.get("visual_analysis") or {}),
        candidate_products=list(row.get("candidate_products") or []),
        match_explanation=dict(row.get("match_explanation") or {}),
        confirmed_by=row.get("confirmed_by"),
        confirmed_at=row.get("confirmed_at"),
        processing_started_at=row.get("processing_started_at"),
        processing_owner=row.get("processing_owner"),
        processing_attempts=int(row.get("processing_attempts") or 0),
        processing_expires_at=row.get("processing_expires_at"),
        last_failure_code=row.get("last_failure_code"),
        next_retry_at=row.get("next_retry_at"),
        first_seen_at=row.get("first_seen_at"),
        last_seen_at=row.get("last_seen_at"),
    )


class StoryProductRepository:
    def get_by_story(
        self,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
    ) -> StoryProductAssociation | None:
        if not tenant_id or not story_media_id or not instagram_account_id:
            raise ValueError("tenant_scoped_lookup_required")
        ensure_tables()
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT *
                        FROM public.instagram_story_products
                        WHERE tenant_id = %s
                          AND provider = %s
                          AND instagram_account_id = %s
                          AND story_media_id = %s
                        LIMIT 1
                        """,
                        (
                            str(tenant_id),
                            str(provider or "brevo"),
                            str(instagram_account_id),
                            str(story_media_id),
                        ),
                    )
                    return _row_to_association(cur.fetchone())
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.get.error]", {"error_type": type(exc).__name__})
            return None

    def create_pending(
        self,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
        story_message_id: str | None = None,
        story_permalink: str | None = None,
        media_type: str = "unknown",
        source_timestamp: datetime | None = None,
        story_expires_at: datetime | None = None,
    ) -> StoryProductAssociation | None:
        if not tenant_id or not story_media_id or not instagram_account_id:
            raise ValueError("tenant_scoped_write_required")
        ensure_tables()
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.instagram_story_products (
                            tenant_id, provider, instagram_account_id, story_media_id,
                            story_message_id, story_permalink, media_type,
                            source_timestamp, story_expires_at,
                            match_source, match_status, match_confidence
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'pending', 'pending', 0
                        )
                        ON CONFLICT (tenant_id, provider, instagram_account_id, story_media_id)
                        DO UPDATE SET
                            last_seen_at = now(),
                            updated_at = now(),
                            story_message_id = COALESCE(
                                EXCLUDED.story_message_id,
                                public.instagram_story_products.story_message_id
                            ),
                            story_permalink = COALESCE(
                                EXCLUDED.story_permalink,
                                public.instagram_story_products.story_permalink
                            )
                        RETURNING *
                        """,
                        (
                            str(tenant_id),
                            str(provider or "brevo"),
                            str(instagram_account_id),
                            str(story_media_id),
                            story_message_id,
                            story_permalink,
                            media_type if media_type in {"image", "video", "carousel", "unknown"} else "unknown",
                            source_timestamp,
                            story_expires_at,
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
            return _row_to_association(row)
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.create.error]", {"error_type": type(exc).__name__})
            return None

    def begin_processing(
        self,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
        owner: str | None = None,
        lease_seconds: int = 90,
    ) -> StoryProductAssociation | None:
        """Atomic claim with lease. Reclaims stale processing leases.

        Returns None when another worker holds a valid lease.
        """
        if not tenant_id or not story_media_id:
            raise ValueError("tenant_scoped_write_required")
        ensure_tables()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=max(15, int(lease_seconds)))
        owner_id = (owner or "worker").strip()[:80] or "worker"
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.instagram_story_products
                        SET match_status = 'processing',
                            processing_started_at = %s,
                            processing_owner = %s,
                            processing_attempts = COALESCE(processing_attempts, 0) + 1,
                            processing_expires_at = %s,
                            last_failure_code = NULL,
                            updated_at = now(),
                            last_seen_at = now()
                        WHERE tenant_id = %s
                          AND provider = %s
                          AND instagram_account_id = %s
                          AND story_media_id = %s
                          AND (
                                match_status IN ('pending', 'failed')
                             OR (
                                    match_status = 'processing'
                                AND (
                                        processing_expires_at IS NULL
                                     OR processing_expires_at < %s
                                )
                             )
                          )
                        RETURNING *
                        """,
                        (
                            now,
                            owner_id,
                            expires,
                            str(tenant_id),
                            str(provider or "brevo"),
                            str(instagram_account_id),
                            str(story_media_id),
                            now,
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
            return _row_to_association(row)
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.begin.error]", {"error_type": type(exc).__name__})
            return None

    def find_visual_analysis_by_hash(
        self,
        *,
        tenant_id: str,
        media_sha256: str,
        analysis_version: str | None = None,
    ) -> StoryVisualUnderstanding | None:
        """L2 persistent visual cache — never includes price/stock authority."""
        if not tenant_id or not media_sha256:
            raise ValueError("tenant_scoped_lookup_required")
        version = analysis_version or str(
            getattr(get_settings(), "instagram_story_analysis_version", "v2") or "v2"
        )
        ensure_tables()
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT visual_analysis, analysis_version
                        FROM public.instagram_story_products
                        WHERE tenant_id = %s
                          AND media_sha256 = %s
                          AND analysis_version = %s
                          AND visual_analysis IS NOT NULL
                          AND visual_analysis <> '{}'::jsonb
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        (str(tenant_id), str(media_sha256), str(version)),
                    )
                    row = cur.fetchone()
            if not row:
                return None
            payload = dict(row.get("visual_analysis") or {})
            payload.pop("price", None)
            payload.pop("stock", None)
            payload.pop("availability", None)
            return StoryVisualUnderstanding.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.visual_cache.error]", {"error_type": type(exc).__name__})
            return None

    def touch_last_seen(
        self,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
    ) -> None:
        if not tenant_id or not story_media_id:
            return
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.instagram_story_products
                        SET last_seen_at = now(), updated_at = now()
                        WHERE tenant_id = %s
                          AND provider = %s
                          AND instagram_account_id = %s
                          AND story_media_id = %s
                        """,
                        (
                            str(tenant_id),
                            str(provider or "brevo"),
                            str(instagram_account_id),
                            str(story_media_id),
                        ),
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.touch.error]", {"error_type": type(exc).__name__})

    def save_visual_analysis(
        self,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
        visual_analysis: dict[str, Any],
        media_sha256: str | None = None,
        media_storage_path: str | None = None,
        analysis_version: str | None = None,
        media_mime: str | None = None,
        media_bytes: int | None = None,
    ) -> None:
        settings = get_settings()
        version = analysis_version or str(
            getattr(settings, "instagram_story_analysis_version", "v2") or "v2"
        )
        retention_days = int(
            getattr(settings, "instagram_story_media_retention_days", 7) or 7
        )
        fields: dict[str, Any] = {
            "visual_analysis": to_jsonb(visual_analysis or {}),
            "media_sha256": media_sha256,
            "media_storage_path": media_storage_path,
            "analysis_version": version,
        }
        if media_mime:
            fields["media_mime"] = media_mime
        if media_bytes is not None:
            fields["media_bytes"] = int(media_bytes)
        if media_storage_path:
            fields["media_expires_at"] = datetime.now(timezone.utc) + timedelta(
                days=retention_days
            )
        self._update_fields(
            tenant_id=tenant_id,
            provider=provider,
            instagram_account_id=instagram_account_id,
            story_media_id=story_media_id,
            fields=fields,
        )

    def save_candidates(
        self,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
        candidates: list[dict[str, Any]],
        explanation: dict[str, Any] | None = None,
    ) -> None:
        self._update_fields(
            tenant_id=tenant_id,
            provider=provider,
            instagram_account_id=instagram_account_id,
            story_media_id=story_media_id,
            fields={
                "candidate_products": to_jsonb(candidates or []),
                "match_explanation": to_jsonb(explanation or {}),
            },
        )

    def confirm_match(
        self,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
        catalog_item_key: str,
        product_id: str,
        variant_id: str | None,
        match_source: str,
        match_confidence: float,
        match_status: str = "matched",
        confirmed_by: str | None = None,
        explanation: dict[str, Any] | None = None,
    ) -> StoryProductAssociation | None:
        ensure_tables()
        now = datetime.now(timezone.utc)
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.instagram_story_products
                        SET catalog_item_key = %s,
                            product_id = %s,
                            variant_id = %s,
                            match_source = %s,
                            match_status = %s,
                            match_confidence = %s,
                            match_explanation = COALESCE(%s::jsonb, match_explanation),
                            confirmed_by = %s,
                            confirmed_at = %s,
                            updated_at = now(),
                            last_seen_at = now()
                        WHERE tenant_id = %s
                          AND provider = %s
                          AND instagram_account_id = %s
                          AND story_media_id = %s
                        RETURNING *
                        """,
                        (
                            catalog_item_key,
                            product_id,
                            variant_id,
                            match_source,
                            match_status,
                            float(match_confidence),
                            to_jsonb(explanation) if explanation is not None else None,
                            confirmed_by,
                            now if confirmed_by or match_status == "manually_confirmed" else None,
                            str(tenant_id),
                            str(provider or "brevo"),
                            str(instagram_account_id),
                            str(story_media_id),
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
            return _row_to_association(row)
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.confirm.error]", {"error_type": type(exc).__name__})
            return None

    def mark_ambiguous(self, **kwargs: Any) -> StoryProductAssociation | None:
        return self._mark_status("ambiguous", **kwargs)

    def mark_not_found(self, **kwargs: Any) -> StoryProductAssociation | None:
        return self._mark_status("not_found", **kwargs)

    def mark_failed(self, **kwargs: Any) -> StoryProductAssociation | None:
        return self._mark_status("failed", **kwargs)

    def mark_expired(self, **kwargs: Any) -> StoryProductAssociation | None:
        return self._mark_status("expired", **kwargs)

    def find_by_media_hash(
        self,
        *,
        tenant_id: str,
        media_sha256: str,
        confirmed_only: bool = True,
    ) -> StoryProductAssociation | None:
        if not tenant_id or not media_sha256:
            raise ValueError("tenant_scoped_lookup_required")
        ensure_tables()
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    status_filter = (
                        "AND match_status IN ('matched', 'manually_confirmed')"
                        if confirmed_only
                        else ""
                    )
                    cur.execute(
                        f"""
                        SELECT *
                        FROM public.instagram_story_products
                        WHERE tenant_id = %s
                          AND media_sha256 = %s
                          {status_filter}
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        (str(tenant_id), str(media_sha256)),
                    )
                    return _row_to_association(cur.fetchone())
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.hash.error]", {"error_type": type(exc).__name__})
            return None

    def list_stories(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        instagram_account_id: str | None = None,
        product_id: str | None = None,
        limit: int = 50,
    ) -> list[StoryProductAssociation]:
        if not tenant_id:
            raise ValueError("tenant_scoped_lookup_required")
        ensure_tables()
        clauses = ["tenant_id = %s"]
        params: list[Any] = [str(tenant_id)]
        if status:
            clauses.append("match_status = %s")
            params.append(status)
        if instagram_account_id:
            clauses.append("instagram_account_id = %s")
            params.append(instagram_account_id)
        if product_id:
            clauses.append("product_id = %s")
            params.append(product_id)
        params.append(max(1, min(200, int(limit))))
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT *
                        FROM public.instagram_story_products
                        WHERE {' AND '.join(clauses)}
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        tuple(params),
                    )
                    rows = cur.fetchall() or []
            return [
                assoc
                for assoc in (_row_to_association(row) for row in rows)
                if assoc is not None
            ]
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.list.error]", {"error_type": type(exc).__name__})
            return []

    def get_by_id(self, *, tenant_id: str, row_id: int) -> StoryProductAssociation | None:
        if not tenant_id:
            raise ValueError("tenant_scoped_lookup_required")
        ensure_tables()
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT *
                        FROM public.instagram_story_products
                        WHERE tenant_id = %s AND id = %s
                        LIMIT 1
                        """,
                        (str(tenant_id), int(row_id)),
                    )
                    return _row_to_association(cur.fetchone())
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.get_id.error]", {"error_type": type(exc).__name__})
            return None

    def unlink(
        self,
        *,
        tenant_id: str,
        row_id: int,
        confirmed_by: str | None = None,
    ) -> StoryProductAssociation | None:
        ensure_tables()
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.instagram_story_products
                        SET catalog_item_key = NULL,
                            product_id = NULL,
                            variant_id = NULL,
                            match_source = 'pending',
                            match_status = 'pending',
                            match_confidence = 0,
                            confirmed_by = %s,
                            confirmed_at = now(),
                            updated_at = now()
                        WHERE tenant_id = %s AND id = %s
                        RETURNING *
                        """,
                        (confirmed_by, str(tenant_id), int(row_id)),
                    )
                    row = cur.fetchone()
                conn.commit()
            return _row_to_association(row)
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.unlink.error]", {"error_type": type(exc).__name__})
            return None

    def _mark_status(
        self,
        status: str,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
        explanation: dict[str, Any] | None = None,
        confidence: float = 0.0,
        failure_code: str | None = None,
    ) -> StoryProductAssociation | None:
        ensure_tables()
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.instagram_story_products
                        SET match_status = %s,
                            match_confidence = %s,
                            match_explanation = COALESCE(%s::jsonb, match_explanation),
                            last_failure_code = COALESCE(%s, last_failure_code),
                            processing_expires_at = CASE
                                WHEN %s IN ('pending', 'failed', 'expired', 'not_found', 'ambiguous', 'matched', 'manually_confirmed')
                                THEN NULL
                                ELSE processing_expires_at
                            END,
                            updated_at = now(),
                            last_seen_at = now()
                        WHERE tenant_id = %s
                          AND provider = %s
                          AND instagram_account_id = %s
                          AND story_media_id = %s
                        RETURNING *
                        """,
                        (
                            status,
                            float(confidence),
                            to_jsonb(explanation) if explanation is not None else None,
                            failure_code
                            or (str((explanation or {}).get("reason") or "")[:80] or None),
                            status,
                            str(tenant_id),
                            str(provider or "brevo"),
                            str(instagram_account_id),
                            str(story_media_id),
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
            return _row_to_association(row)
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.mark.error]", {"error_type": type(exc).__name__, "status": status})
            return None

    def _update_fields(
        self,
        *,
        tenant_id: str,
        provider: str,
        instagram_account_id: str,
        story_media_id: str,
        fields: dict[str, Any],
    ) -> None:
        if not fields:
            return
        ensure_tables()
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            if value is None and key not in {"media_sha256", "media_storage_path"}:
                continue
            assignments.append(f"{key} = %s")
            values.append(value)
        if not assignments:
            return
        assignments.append("updated_at = now()")
        values.extend(
            [
                str(tenant_id),
                str(provider or "brevo"),
                str(instagram_account_id),
                str(story_media_id),
            ]
        )
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE public.instagram_story_products
                        SET {', '.join(assignments)}
                        WHERE tenant_id = %s
                          AND provider = %s
                          AND instagram_account_id = %s
                          AND story_media_id = %s
                        """,
                        tuple(values),
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            print("[story.repo.update.error]", {"error_type": type(exc).__name__})
