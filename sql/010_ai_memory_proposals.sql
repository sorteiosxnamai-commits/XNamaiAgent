-- Memory proposals, contact memories, instruction extensions, conversation summaries.
-- Idempotent migration (Phase 5: audit-only proposals; no auto-apply by default).

CREATE TABLE IF NOT EXISTS public.ai_agent_instruction_extensions (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,

    scope text NOT NULL
        CHECK (scope IN ('tenant', 'channel', 'contact')),

    scope_key text,
    scope_key_norm text NOT NULL DEFAULT '',

    extension_key text NOT NULL,
    category text NOT NULL,

    instruction_text text NOT NULL,
    instruction_hash text NOT NULL,

    source text NOT NULL
        CHECK (
            source IN (
                'user',
                'model_proposal',
                'migration',
                'system'
            )
        ),

    status text NOT NULL DEFAULT 'pending_review'
        CHECK (
            status IN (
                'pending_review',
                'active',
                'rejected',
                'superseded',
                'expired'
            )
        ),

    importance numeric(5,4),
    confidence numeric(5,4),

    evidence_count integer NOT NULL DEFAULT 1,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),

    proposed_by_response_id bigint,
    proposed_by_inbound_id bigint,

    approved_by text,
    approved_at timestamptz,
    rejected_by text,
    rejected_at timestamptz,
    rejection_reason text,

    expires_at timestamptz,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_instruction_extensions_active
ON public.ai_agent_instruction_extensions(
    tenant_id,
    scope,
    scope_key_norm,
    status
);

CREATE INDEX IF NOT EXISTS idx_ai_instruction_extensions_pending
ON public.ai_agent_instruction_extensions(
    tenant_id,
    status,
    created_at DESC
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_instruction_extension_active_key
ON public.ai_agent_instruction_extensions(
    tenant_id,
    scope,
    scope_key_norm,
    extension_key
)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS public.ai_contact_memories (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,
    sender_key text NOT NULL,

    memory_key text NOT NULL,
    memory_kind text NOT NULL,

    value jsonb NOT NULL DEFAULT '{}'::jsonb,
    safe_summary text,

    source text NOT NULL DEFAULT 'model_proposal'
        CHECK (
            source IN (
                'explicit_user',
                'model_proposal',
                'legacy',
                'admin',
                'system'
            )
        ),

    status text NOT NULL DEFAULT 'active'
        CHECK (
            status IN (
                'pending',
                'active',
                'superseded',
                'forgotten',
                'rejected',
                'expired'
            )
        ),

    importance numeric(5,4) NOT NULL DEFAULT 0,
    confidence numeric(5,4) NOT NULL DEFAULT 0,

    use_in_instructions boolean NOT NULL DEFAULT false,
    sensitive boolean NOT NULL DEFAULT false,

    source_inbound_id bigint,
    source_response_id bigint,

    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_confirmed_at timestamptz,
    expires_at timestamptz,

    superseded_by_id bigint,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_contact_memories_active
ON public.ai_contact_memories(
    tenant_id,
    sender_key,
    status,
    importance DESC
);

CREATE INDEX IF NOT EXISTS idx_ai_contact_memories_key
ON public.ai_contact_memories(
    tenant_id,
    sender_key,
    memory_key,
    status
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_contact_memory_active_key
ON public.ai_contact_memories(
    tenant_id,
    sender_key,
    memory_key
)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS public.ai_conversation_summaries (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,
    conversation_key text NOT NULL,

    version bigint NOT NULL DEFAULT 1,

    current_goal text,
    summary text,

    resolved_points jsonb NOT NULL DEFAULT '[]'::jsonb,
    open_questions jsonb NOT NULL DEFAULT '[]'::jsonb,
    user_corrections jsonb NOT NULL DEFAULT '[]'::jsonb,
    commitments jsonb NOT NULL DEFAULT '[]'::jsonb,

    last_failure text,
    last_inbound_id bigint,
    last_response_id bigint,

    approximate_token_count integer NOT NULL DEFAULT 0,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, conversation_key)
);

CREATE INDEX IF NOT EXISTS idx_ai_conversation_summaries_updated
ON public.ai_conversation_summaries(
    tenant_id,
    updated_at DESC
);

CREATE TABLE IF NOT EXISTS public.ai_memory_proposals (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,
    conversation_key text,
    sender_key text,

    inbound_id bigint,
    response_id bigint,

    proposal_type text NOT NULL
        CHECK (
            proposal_type IN (
                'contact_memory',
                'conversation_memory',
                'instruction_extension',
                'forget_memory',
                'summary_delta'
            )
        ),

    target_scope text NOT NULL,
    proposal_key text,

    proposed_value jsonb NOT NULL DEFAULT '{}'::jsonb,
    proposed_text text,

    importance numeric(5,4),
    confidence numeric(5,4),

    reason_code text,
    sensitive_detected boolean NOT NULL DEFAULT false,

    status text NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'approved',
                'applied',
                'rejected',
                'duplicate',
                'superseded'
            )
        ),

    idempotency_key text NOT NULL UNIQUE,

    applied_memory_id bigint,
    applied_extension_id bigint,

    rejection_codes jsonb NOT NULL DEFAULT '[]'::jsonb,

    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    applied_at timestamptz,

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_memory_proposals_review
ON public.ai_memory_proposals(
    tenant_id,
    status,
    created_at DESC
);

CREATE INDEX IF NOT EXISTS idx_ai_memory_proposals_conversation
ON public.ai_memory_proposals(
    tenant_id,
    conversation_key,
    created_at DESC
);
