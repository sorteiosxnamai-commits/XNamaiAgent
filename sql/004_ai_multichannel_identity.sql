ALTER TABLE public.ai_inbound_messages
    ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS sender_key text,
    ADD COLUMN IF NOT EXISTS sender_external_id text,
    ADD COLUMN IF NOT EXISTS visitor_id text,
    ADD COLUMN IF NOT EXISTS sender_username text,
    ADD COLUMN IF NOT EXISTS source_channel_ref text,
    ADD COLUMN IF NOT EXISTS source_channel_link text,
    ADD COLUMN IF NOT EXISTS source_conversation_ref text,
    ADD COLUMN IF NOT EXISTS channel_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_ai_inbound_sender_key_created_at
ON public.ai_inbound_messages(sender_key, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_inbound_channel_created_at
ON public.ai_inbound_messages(channel, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_inbound_visitor_id
ON public.ai_inbound_messages(visitor_id);

CREATE INDEX IF NOT EXISTS idx_ai_inbound_source_conversation_ref
ON public.ai_inbound_messages(channel, source_conversation_ref);

CREATE INDEX IF NOT EXISTS idx_ai_inbound_conversation_created_at
ON public.ai_inbound_messages(conversation_id, created_at DESC);

WITH ranked_messages AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY provider, message_id
            ORDER BY id
        ) AS occurrence
    FROM public.ai_inbound_messages
    WHERE message_id IS NOT NULL
)
UPDATE public.ai_inbound_messages AS inbound
SET message_id = NULL
FROM ranked_messages AS ranked
WHERE inbound.id = ranked.id
  AND ranked.occurrence > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_inbound_provider_message_id
ON public.ai_inbound_messages(provider, message_id)
WHERE message_id IS NOT NULL;

ALTER TABLE public.ai_agent_responses
    ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS sender_key text;

CREATE INDEX IF NOT EXISTS idx_ai_agent_responses_sender_key_created_at
ON public.ai_agent_responses(sender_key, created_at DESC);

UPDATE public.ai_inbound_messages
SET channel = 'whatsapp'
WHERE sender_phone IS NOT NULL
  AND channel = 'unknown';

UPDATE public.ai_inbound_messages
SET sender_key =
    'whatsapp:' || regexp_replace(sender_phone, '[^0-9]', '', 'g')
WHERE sender_key IS NULL
  AND sender_phone IS NOT NULL
  AND regexp_replace(sender_phone, '[^0-9]', '', 'g') <> '';
