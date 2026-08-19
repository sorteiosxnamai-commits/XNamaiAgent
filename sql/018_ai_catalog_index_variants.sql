-- 018: variant-safe catalog index key (non-destructive vs 017).
-- Preserves existing rows; adds catalog_item_key UNIQUE (tenant_id, catalog_item_key).
-- Rollback: drop unique index/constraint and column catalog_item_key (see docs).

ALTER TABLE public.ai_catalog_index
    ADD COLUMN IF NOT EXISTS catalog_item_key text;

-- Backfill: prefer variant:{id} when variant_id present, else product:{product_id}.
UPDATE public.ai_catalog_index
SET catalog_item_key = CASE
    WHEN variant_id IS NOT NULL AND btrim(variant_id) <> '' THEN 'variant:' || btrim(variant_id)
    ELSE 'product:' || btrim(product_id)
END
WHERE catalog_item_key IS NULL OR btrim(catalog_item_key) = '';

-- Deduplicate deterministically (keep newest freshness_at / updated_at).
DELETE FROM public.ai_catalog_index a
USING public.ai_catalog_index b
WHERE a.ctid < b.ctid
  AND a.tenant_id = b.tenant_id
  AND a.catalog_item_key = b.catalog_item_key;

ALTER TABLE public.ai_catalog_index
    ALTER COLUMN catalog_item_key SET NOT NULL;

-- Drop legacy PK if still (tenant_id, product_id); keep product_id indexed.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ai_catalog_index_pkey'
          AND conrelid = 'public.ai_catalog_index'::regclass
    ) THEN
        ALTER TABLE public.ai_catalog_index DROP CONSTRAINT ai_catalog_index_pkey;
    END IF;
EXCEPTION WHEN undefined_table THEN
    NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_catalog_index_tenant_item
    ON public.ai_catalog_index (tenant_id, catalog_item_key);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_tenant_product
    ON public.ai_catalog_index (tenant_id, product_id);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_tenant_variant
    ON public.ai_catalog_index (tenant_id, variant_id)
    WHERE variant_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_tenant_sku
    ON public.ai_catalog_index (tenant_id, sku)
    WHERE sku IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_tenant_ean
    ON public.ai_catalog_index (tenant_id, ean)
    WHERE ean IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_tenant_reference
    ON public.ai_catalog_index (tenant_id, reference)
    WHERE reference IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_tenant_brand
    ON public.ai_catalog_index (tenant_id, brand);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_index_tenant_freshness
    ON public.ai_catalog_index (tenant_id, freshness_at DESC);
