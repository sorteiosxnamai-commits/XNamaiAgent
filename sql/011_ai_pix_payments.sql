-- PIX payments created by the agent via Mercado Pago (Phase 2).
-- Idempotent migration. Settlement/Tray order creation comes in Phase 3.

CREATE TABLE IF NOT EXISTS public.ai_pix_payments (
    id bigserial PRIMARY KEY,

    mp_payment_id text NOT NULL,
    status text NOT NULL DEFAULT 'pending',

    amount_cents integer NOT NULL,
    currency text NOT NULL DEFAULT 'BRL',
    description text,
    payer_email text,

    qr_code text,
    qr_code_base64 text,
    external_reference text,
    date_of_expiration timestamptz,
    expires_at timestamptz,

    conversation_id text,
    sender_key text,
    sender_phone text,
    channel text,
    cart_session_id text,
    checkout_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    paid_at timestamptz,
    settlement_status text NOT NULL DEFAULT 'none'
        CHECK (
            settlement_status IN (
                'none',
                'pending',
                'processing',
                'completed',
                'failed',
                'skipped'
            )
        ),
    tray_order_id text,
    settled_at timestamptz,
    settlement_error text,

    last_webhook_at timestamptz,
    raw_create jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_last_status jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_ai_pix_payments_mp_payment_id UNIQUE (mp_payment_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_pix_payments_status_created
ON public.ai_pix_payments(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_pix_payments_sender_key
ON public.ai_pix_payments(sender_key);

CREATE INDEX IF NOT EXISTS idx_ai_pix_payments_conversation_id
ON public.ai_pix_payments(conversation_id);

CREATE INDEX IF NOT EXISTS idx_ai_pix_payments_settlement
ON public.ai_pix_payments(settlement_status, status)
WHERE status = 'approved';

CREATE INDEX IF NOT EXISTS idx_ai_pix_payments_external_reference
ON public.ai_pix_payments(external_reference);
