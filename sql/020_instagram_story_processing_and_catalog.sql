-- 020: Story processing lease + analysis version + media retention metadata
--        + catalog index dedupe by freshness (fix for 018 ctid ordering).
-- Apply after 019. Non-destructive ADD COLUMN / indexes.
-- Rollback notes at bottom.

BEGIN;

-- Processing lease / recovery
ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS analysis_version text NULL;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS processing_started_at timestamptz NULL;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS processing_owner text NULL;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS processing_attempts integer NOT NULL DEFAULT 0;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS processing_expires_at timestamptz NULL;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS last_failure_code text NULL;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS next_retry_at timestamptz NULL;

-- Private media metadata (no signed URLs)
ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS media_mime text NULL;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS media_bytes integer NULL;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS media_deleted_at timestamptz NULL;

ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS media_expires_at timestamptz NULL;

-- Structured carousel / multi-item snapshot (validated in app)
ALTER TABLE public.instagram_story_products
    ADD COLUMN IF NOT EXISTS media_items jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_instagram_story_products_sha256
ON public.instagram_story_products (tenant_id, media_sha256)
WHERE media_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_instagram_story_products_analysis_version
ON public.instagram_story_products (tenant_id, media_sha256, analysis_version)
WHERE media_sha256 IS NOT NULL AND analysis_version IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_instagram_story_products_media_expiry
ON public.instagram_story_products (media_expires_at)
WHERE media_storage_path IS NOT NULL AND media_deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_instagram_story_products_processing_lease
ON public.instagram_story_products (tenant_id, match_status, processing_expires_at)
WHERE match_status = 'processing';

-- Catalog: keep newest by freshness_at / updated_at (018 used ctid).
-- ai_catalog_index has freshness_at + updated_at only (no created_at).
DELETE FROM public.ai_catalog_index a
USING public.ai_catalog_index b
WHERE a.tenant_id = b.tenant_id
  AND a.catalog_item_key = b.catalog_item_key
  AND a.ctid <> b.ctid
  AND (
        coalesce(a.freshness_at, a.updated_at)
      < coalesce(b.freshness_at, b.updated_at)
      OR (
            coalesce(a.freshness_at, a.updated_at)
          = coalesce(b.freshness_at, b.updated_at)
        AND a.ctid < b.ctid
      )
  );

COMMIT;

-- Rollback (manual, destructive):
-- ALTER TABLE public.instagram_story_products
--   DROP COLUMN IF EXISTS analysis_version,
--   DROP COLUMN IF EXISTS processing_started_at,
--   DROP COLUMN IF EXISTS processing_owner,
--   DROP COLUMN IF EXISTS processing_attempts,
--   DROP COLUMN IF EXISTS processing_expires_at,
--   DROP COLUMN IF EXISTS last_failure_code,
--   DROP COLUMN IF EXISTS next_retry_at,
--   DROP COLUMN IF EXISTS media_mime,
--   DROP COLUMN IF EXISTS media_bytes,
--   DROP COLUMN IF EXISTS media_deleted_at,
--   DROP COLUMN IF EXISTS media_expires_at,
--   DROP COLUMN IF EXISTS media_items;
-- DROP INDEX IF EXISTS idx_instagram_story_products_sha256;
-- DROP INDEX IF EXISTS idx_instagram_story_products_analysis_version;
-- DROP INDEX IF EXISTS idx_instagram_story_products_media_expiry;
-- DROP INDEX IF EXISTS idx_instagram_story_products_processing_lease;
