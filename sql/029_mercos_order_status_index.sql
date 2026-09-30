BEGIN;
CREATE TABLE IF NOT EXISTS public.ai_mercos_order_status (
    tenant_id text NOT NULL,
    mercos_id text NOT NULL,
    order_number text NOT NULL,
    status_code text,
    billing_code text,
    excluded boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, mercos_id)
);
CREATE INDEX IF NOT EXISTS ai_mercos_order_status_number
    ON public.ai_mercos_order_status (tenant_id, order_number);
CREATE TABLE IF NOT EXISTS public.ai_mercos_order_sync (
    tenant_id text PRIMARY KEY,
    cursor_value text,
    window_start timestamptz NOT NULL,
    completed_at timestamptz
);
ALTER TABLE public.ai_mercos_order_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_mercos_order_sync ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_mercos_order_status, public.ai_mercos_order_sync FROM anon, authenticated;
COMMIT;
