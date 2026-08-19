-- 021: Story association audit + catalog TTL index.
-- Apply after 020. Non-destructive.
-- Note: CONCURRENTLY omitted for transactional migration runners.

BEGIN;

CREATE TABLE IF NOT EXISTS public.story_product_association_audit (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL,
    story_row_id bigint NULL,
    story_media_id text NULL,
    actor_id text NOT NULL,
    action text NOT NULL,
    previous_product_id text NULL,
    previous_variant_id text NULL,
    previous_catalog_item_key text NULL,
    new_product_id text NULL,
    new_variant_id text NULL,
    new_catalog_item_key text NULL,
    reason text NULL,
    request_trace_id text NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_story_assoc_audit_tenant_created
ON public.story_product_association_audit (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_tenant_updated
ON public.ai_catalog_index (tenant_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_catalog_tenant_freshness
ON public.ai_catalog_index (tenant_id, freshness_at DESC);

COMMIT;

-- Rollback:
-- DROP TABLE IF EXISTS public.story_product_association_audit;
-- DROP INDEX IF EXISTS idx_ai_catalog_tenant_updated;
-- DROP INDEX IF EXISTS idx_ai_catalog_tenant_freshness;
