BEGIN;
-- NULL for old rows: migration must not pretend their status was just verified.
ALTER TABLE public.ai_mercos_order_status ADD COLUMN IF NOT EXISTS verified_at timestamptz;
ALTER TABLE public.ai_mercos_order_sync ADD COLUMN IF NOT EXISTS history_cursor text;
ALTER TABLE public.ai_mercos_order_sync ADD COLUMN IF NOT EXISTS history_completed_at timestamptz;
-- Both tables remain private to the backend.
ALTER TABLE public.ai_mercos_order_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_mercos_order_sync ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_mercos_order_status, public.ai_mercos_order_sync FROM anon, authenticated;
COMMIT;
