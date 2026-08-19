BEGIN;

CREATE TABLE IF NOT EXISTS public.ai_remarketing_contacts (
    id bigserial PRIMARY KEY,
    channel text NOT NULL,
    identity_key text NOT NULL,
    sender_key text,
    sender_external_id text,
    visitor_id text,
    conversation_id text,
    source_conversation_ref text,
    sender_phone text,
    sender_name text,
    marketing_status text NOT NULL DEFAULT 'eligible',
    last_customer_message_at timestamptz NOT NULL,
    messaging_window_expires_at timestamptz NOT NULL,
    opted_out_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_ai_remarketing_contacts_status
        CHECK (marketing_status IN ('eligible', 'opted_out'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_remarketing_contacts_identity
ON public.ai_remarketing_contacts(channel, identity_key);

CREATE INDEX IF NOT EXISTS idx_ai_remarketing_contacts_sender_key
ON public.ai_remarketing_contacts(sender_key);

CREATE TABLE IF NOT EXISTS public.ai_conversation_statuses (
    id bigserial PRIMARY KEY,
    contact_id bigint NOT NULL
        REFERENCES public.ai_remarketing_contacts(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'active',
    stage text NOT NULL DEFAULT 'commercial_interest',
    last_inbound_id bigint
        REFERENCES public.ai_inbound_messages(id) ON DELETE SET NULL,
    cart_session_id text,
    cart_url text,
    order_id text,
    payment_url text,
    product_name text,
    last_customer_message_at timestamptz NOT NULL,
    next_scheduled_at timestamptz,
    completed_at timestamptz,
    completion_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_ai_conversation_status
        CHECK (status IN ('active', 'completed', 'cancelled', 'expired')),
    CONSTRAINT ck_ai_conversation_stage
        CHECK (stage IN (
            'commercial_interest',
            'product_selection',
            'cart',
            'checkout',
            'awaiting_payment'
        ))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_conversation_one_active
ON public.ai_conversation_statuses(contact_id)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_ai_conversation_status_due
ON public.ai_conversation_statuses(status, next_scheduled_at)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS public.ai_remarketing_attempts (
    id bigserial PRIMARY KEY,
    conversation_status_id bigint NOT NULL
        REFERENCES public.ai_conversation_statuses(id) ON DELETE CASCADE,
    touch_number integer NOT NULL,
    scheduled_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempt_count integer NOT NULL DEFAULT 0,
    claimed_at timestamptz,
    sent_at timestamptz,
    message_text text,
    provider_send_ok boolean,
    provider_response jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ai_remarketing_attempt_touch
        UNIQUE (conversation_status_id, touch_number),
    CONSTRAINT ck_ai_remarketing_attempt_status
        CHECK (status IN (
            'pending',
            'processing',
            'sent',
            'failed',
            'cancelled',
            'expired'
        ))
);

CREATE INDEX IF NOT EXISTS idx_ai_remarketing_attempts_due
ON public.ai_remarketing_attempts(status, scheduled_at)
WHERE status IN ('pending', 'processing');

COMMIT;
