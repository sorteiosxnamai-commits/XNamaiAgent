-- Attendance self-learning: reviews + insights that feed persona/knowledge.
-- Idempotent migration.

CREATE TABLE IF NOT EXISTS public.ai_attendance_reviews (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,
    conversation_key text,
    sender_key text,

    inbound_id bigint,
    response_id bigint,

    channel text,
    customer_text text,
    agent_reply text,

    outcome text NOT NULL DEFAULT 'reviewed'
        CHECK (
            outcome IN (
                'reviewed',
                'success',
                'failure',
                'handoff',
                'empty_catalog',
                'duplicate_greeting',
                'policy_miss',
                'unclear'
            )
        ),

    failure_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    signals jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_attendance_reviews_created
ON public.ai_attendance_reviews (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_attendance_reviews_outcome
ON public.ai_attendance_reviews (tenant_id, outcome, created_at DESC);

CREATE TABLE IF NOT EXISTS public.ai_learning_insights (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,

    insight_key text NOT NULL,
    category text NOT NULL
        CHECK (
            category IN (
                'persona',
                'knowledge',
                'retrieval',
                'handoff',
                'greeting',
                'policy',
                'other'
            )
        ),

    title text NOT NULL,
    insight_text text NOT NULL,

    evidence_count integer NOT NULL DEFAULT 1,
    confidence numeric(5,4) NOT NULL DEFAULT 0.5,
    importance numeric(5,4) NOT NULL DEFAULT 0.5,

    status text NOT NULL DEFAULT 'pending_review'
        CHECK (
            status IN (
                'pending_review',
                'applied',
                'rejected',
                'superseded',
                'expired'
            )
        ),

    applied_extension_id bigint,
    source_review_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    expires_at timestamptz,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_learning_insight_pending_key
ON public.ai_learning_insights (tenant_id, insight_key)
WHERE status = 'pending_review';

CREATE INDEX IF NOT EXISTS idx_ai_learning_insights_active_review
ON public.ai_learning_insights (tenant_id, status, importance DESC, created_at DESC);
