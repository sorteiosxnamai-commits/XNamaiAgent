-- Local idle tracking for ChatBô human takeover (agent pause is not permanent).

CREATE TABLE IF NOT EXISTS public.ai_human_takeover_state (
    state_key text PRIMARY KEY,
    conversation_key text,
    sender_key text,
    last_human_activity_at timestamptz NOT NULL DEFAULT now(),
    takeover_detected_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_human_takeover_activity
ON public.ai_human_takeover_state (last_human_activity_at DESC);
