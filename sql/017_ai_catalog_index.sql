-- Canonical catalog index for hybrid retrieval (Etapa 4).
-- Tenant-scoped; commercial facts still revalidated live before display.

CREATE TABLE IF NOT EXISTS public.ai_catalog_index (
    tenant_id text NOT NULL DEFAULT 'newstore',
    product_id text NOT NULL,
    variant_id text NULL,
    sku text NULL,
    ean text NULL,
    reference text NULL,
    brand text NULL,
    collection text NULL,
    model text NULL,
    title_normalized text NOT NULL DEFAULT '',
    category text NULL,
    gender text NULL,
    mechanism text NULL,
    case_size text NULL,
    dial_color text NULL,
    strap_color text NULL,
    material text NULL,
    strap_type text NULL,
    colors_normalized jsonb NOT NULL DEFAULT '[]'::jsonb,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    price numeric NULL,
    promotional_price numeric NULL,
    stock integer NULL,
    available boolean NULL,
    available_in_store boolean NULL,
    url text NULL,
    image_url text NULL,
    freshness_at timestamptz NOT NULL DEFAULT now(),
    factual_source text NOT NULL DEFAULT 'tray_search',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_brand
ON public.ai_catalog_index (tenant_id, brand);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_ean
ON public.ai_catalog_index (tenant_id, ean)
WHERE ean IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_reference
ON public.ai_catalog_index (tenant_id, reference)
WHERE reference IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_title
ON public.ai_catalog_index (tenant_id, title_normalized);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_freshness
ON public.ai_catalog_index (freshness_at DESC);
