ALTER TABLE public.ai_inbound_messages
  ADD COLUMN IF NOT EXISTS workspace_id uuid,
  ADD COLUMN IF NOT EXISTS chatbo_synced_at timestamptz,
  ADD COLUMN IF NOT EXISTS chatbo_sync_error text;

ALTER TABLE public.ai_agent_responses
  ADD COLUMN IF NOT EXISTS workspace_id uuid,
  ADD COLUMN IF NOT EXISTS chatbo_synced_at timestamptz,
  ADD COLUMN IF NOT EXISTS chatbo_sync_error text;

CREATE INDEX IF NOT EXISTS idx_ai_inbound_chatbo_sync_pending
  ON public.ai_inbound_messages (provider, id)
  WHERE chatbo_synced_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_ai_response_chatbo_sync_pending
  ON public.ai_agent_responses (inbound_id, id)
  WHERE chatbo_synced_at IS NULL;
