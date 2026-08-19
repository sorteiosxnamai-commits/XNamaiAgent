CREATE TABLE IF NOT EXISTS public.ai_customer_commerce_sessions (
  person_key text PRIMARY KEY,
  commerce_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  channel text NULL,
  conversation_id text NULL,
  sender_key text NULL,
  sender_phone text NULL,
  resumable_score integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_updated_at
ON public.ai_customer_commerce_sessions(updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_sender_key
ON public.ai_customer_commerce_sessions(sender_key);

CREATE INDEX IF NOT EXISTS idx_ai_customer_commerce_sessions_sender_phone
ON public.ai_customer_commerce_sessions(sender_phone);
