-- Versioned user-managed agent persona + prompt compilation audit.
-- Idempotent migration.

CREATE TABLE IF NOT EXISTS public.ai_agent_persona_versions (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,
    persona_key text NOT NULL,

    version integer NOT NULL,
    name text NOT NULL,

    source text NOT NULL DEFAULT 'user'
        CHECK (source IN ('user', 'migration', 'system')),

    instructions text NOT NULL,
    instructions_hash text NOT NULL,

    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'archived')),

    created_by text,
    activated_by text,

    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    archived_at timestamptz,

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    UNIQUE (tenant_id, persona_key, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_agent_persona_active
ON public.ai_agent_persona_versions(tenant_id, persona_key)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_ai_agent_persona_versions_lookup
ON public.ai_agent_persona_versions(
    tenant_id,
    persona_key,
    status,
    version DESC
);

CREATE TABLE IF NOT EXISTS public.ai_prompt_compilations (
    id bigserial PRIMARY KEY,

    tenant_id text NOT NULL,
    conversation_key text,
    sender_key text,

    inbound_id bigint,
    response_id bigint,

    persona_version_id bigint,
    instruction_extension_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    contact_memory_ids jsonb NOT NULL DEFAULT '[]'::jsonb,

    compiled_instructions_hash text NOT NULL,

    instructions_char_count integer NOT NULL DEFAULT 0,
    input_char_count integer NOT NULL DEFAULT 0,
    approximate_input_tokens integer NOT NULL DEFAULT 0,

    channel text,
    openai_api_mode text NOT NULL,

    created_at timestamptz NOT NULL DEFAULT now(),

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_ai_prompt_compilations_inbound
ON public.ai_prompt_compilations(inbound_id);

CREATE INDEX IF NOT EXISTS idx_ai_prompt_compilations_conversation
ON public.ai_prompt_compilations(
    tenant_id,
    conversation_key,
    created_at DESC
);
