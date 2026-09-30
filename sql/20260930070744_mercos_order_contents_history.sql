-- Orders indexed before contents existed need a new historical pass too.
-- Keep the live cursor and freshness unchanged; background work fills NULL
-- contents without replacing the latest known status or deletion.
UPDATE public.ai_mercos_order_sync
SET history_cursor = NULL, history_completed_at = NULL
WHERE tenant_id = 'xnamai' AND EXISTS (
  SELECT 1 FROM public.ai_mercos_order_status o
  WHERE o.tenant_id = ai_mercos_order_sync.tenant_id AND o.contents IS NULL
);
