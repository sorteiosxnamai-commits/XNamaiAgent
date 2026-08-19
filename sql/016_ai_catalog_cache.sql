-- Compact Tray catalog pools (brand/category) for fast preference filtering.

CREATE TABLE IF NOT EXISTS public.ai_catalog_cache (
    cache_key text PRIMARY KEY,
    products jsonb NOT NULL DEFAULT '[]'::jsonb,
    product_count integer NOT NULL DEFAULT 0,
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_cache_expires
ON public.ai_catalog_cache (expires_at);
