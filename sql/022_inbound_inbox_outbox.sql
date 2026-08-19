-- 022: Durable inbound inbox + outbound outbox for async webhook processing.
-- Apply after 021. Non-destructive.
-- Enables: validate → enqueue → HTTP 200, then worker processes turns.

BEGIN;

CREATE TABLE IF NOT EXISTS public.ai_inbound_inbox (
    id bigserial PRIMARY KEY,
    provider text NOT NULL,
    channel text NOT NULL DEFAULT 'unknown',
    message_id text NULL,
    idempotency_key text NOT NULL,
    conversation_key text NULL,
    visitor_id text NULL,
    sender_key text NULL,
    event_name text NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',
            'leased',
            'processed',
            'failed',
            'dead',
            'skipped'
        )),
    attempts int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 8,
    lease_owner text NULL,
    lease_expires_at timestamptz NULL,
    last_error text NULL,
    processed_inbound_id bigint NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_inbound_inbox_idempotency
ON public.ai_inbound_inbox (idempotency_key);

CREATE INDEX IF NOT EXISTS idx_ai_inbound_inbox_status_created
ON public.ai_inbound_inbox (status, created_at ASC)
WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_ai_inbound_inbox_lease
ON public.ai_inbound_inbox (lease_expires_at)
WHERE status = 'leased';

CREATE TABLE IF NOT EXISTS public.ai_outbound_outbox (
    id bigserial PRIMARY KEY,
    inbox_id bigint NULL REFERENCES public.ai_inbound_inbox(id) ON DELETE SET NULL,
    inbound_id bigint NULL,
    provider text NOT NULL,
    channel text NOT NULL DEFAULT 'unknown',
    conversation_key text NULL,
    visitor_id text NULL,
    sender_key text NULL,
    recipient_external_id text NULL,
    reply_text text NOT NULL DEFAULT '',
    reply_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',
            'leased',
            'sent',
            'failed',
            'dead',
            'skipped'
        )),
    attempts int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 8,
    lease_owner text NULL,
    lease_expires_at timestamptz NULL,
    last_error text NULL,
    provider_response jsonb NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_outbound_outbox_status_created
ON public.ai_outbound_outbox (status, created_at ASC)
WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS idx_ai_outbound_outbox_inbound
ON public.ai_outbound_outbox (inbound_id)
WHERE inbound_id IS NOT NULL;

COMMIT;

-- Rollback:
-- DROP TABLE IF EXISTS public.ai_outbound_outbox;
-- DROP TABLE IF EXISTS public.ai_inbound_inbox;
