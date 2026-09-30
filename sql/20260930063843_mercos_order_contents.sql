ALTER TABLE public.ai_mercos_order_status ADD COLUMN IF NOT EXISTS contents jsonb;
COMMENT ON COLUMN public.ai_mercos_order_status.contents IS
  'Allowlisted Mercos order items and totals. NULL means contents not confirmed; no customer PII.';
ALTER TABLE public.ai_mercos_order_status ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_mercos_order_status FROM anon, authenticated;

-- Re-read the already indexed recent window once to hydrate existing orders.
UPDATE public.ai_mercos_order_sync
SET cursor_value = to_char(window_start AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS'),
    completed_at = NULL
WHERE tenant_id = 'xnamai' AND EXISTS (
  SELECT 1 FROM public.ai_mercos_order_status o
  WHERE o.tenant_id = ai_mercos_order_sync.tenant_id AND o.contents IS NULL
);
