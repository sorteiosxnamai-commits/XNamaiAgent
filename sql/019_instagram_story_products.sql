-- 019_instagram_story_products.sql
-- Story ↔ product association (tenant-scoped). Non-destructive vs prior migrations.
-- Apply after 018. Rollback: DROP TABLE public.instagram_story_products;

BEGIN;

CREATE TABLE IF NOT EXISTS public.instagram_story_products (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,
    provider text NOT NULL DEFAULT 'brevo',
    instagram_account_id text NOT NULL,
    story_media_id text NOT NULL,

    story_message_id text NULL,
    story_permalink text NULL,
    media_type text NOT NULL DEFAULT 'unknown',
    source_timestamp timestamptz NULL,
    story_expires_at timestamptz NULL,

    media_storage_path text NULL,
    media_sha256 text NULL,
    thumbnail_sha256 text NULL,

    catalog_item_key text NULL,
    product_id text NULL,
    variant_id text NULL,

    match_source text NOT NULL DEFAULT 'pending',
    match_status text NOT NULL DEFAULT 'pending',
    match_confidence numeric(6,5) NOT NULL DEFAULT 0,

    visual_analysis jsonb NOT NULL DEFAULT '{}'::jsonb,
    candidate_products jsonb NOT NULL DEFAULT '[]'::jsonb,
    match_explanation jsonb NOT NULL DEFAULT '{}'::jsonb,

    confirmed_by text NULL,
    confirmed_at timestamptz NULL,

    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, provider, instagram_account_id, story_media_id),

    CONSTRAINT instagram_story_products_media_type_chk
      CHECK (media_type IN ('image', 'video', 'carousel', 'unknown')),
    CONSTRAINT instagram_story_products_match_source_chk
      CHECK (match_source IN (
        'publication_metadata',
        'manual',
        'visual_exact_reference',
        'visual_catalog_match',
        'visual_similarity',
        'pending'
      )),
    CONSTRAINT instagram_story_products_match_status_chk
      CHECK (match_status IN (
        'pending',
        'processing',
        'matched',
        'ambiguous',
        'not_found',
        'failed',
        'expired',
        'manually_confirmed'
      ))
);

CREATE INDEX IF NOT EXISTS idx_instagram_story_products_lookup
ON public.instagram_story_products (
    tenant_id,
    instagram_account_id,
    story_media_id
);

CREATE INDEX IF NOT EXISTS idx_instagram_story_products_product
ON public.instagram_story_products (
    tenant_id,
    product_id,
    variant_id
);

CREATE INDEX IF NOT EXISTS idx_instagram_story_products_status
ON public.instagram_story_products (
    tenant_id,
    match_status,
    updated_at DESC
);

CREATE INDEX IF NOT EXISTS idx_instagram_story_products_sha
ON public.instagram_story_products (
    tenant_id,
    media_sha256
)
WHERE media_sha256 IS NOT NULL;

COMMIT;

-- Rollback (manual):
-- DROP TABLE IF EXISTS public.instagram_story_products;
