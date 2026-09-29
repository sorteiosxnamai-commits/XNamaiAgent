-- 025_mercos_customer_document_index.sql
--
-- Forward migration. Nothing here alters an earlier migration.
--
-- Private index used to answer "is there already a Mercos customer with this
-- CPF/CNPJ?" before the agent creates one. The MercosAdaptor has no lookup by
-- document (GET /v1/customers only accepts `alterado_apos`), so the agent keeps
-- an incremental index fed by that listing.
--
-- No raw CPF/CNPJ and no customer payload is stored: only an HMAC-SHA-256
-- digest keyed by CUSTOMER_DOCUMENT_HMAC_KEY (which never reaches the database).

CREATE TABLE IF NOT EXISTS public.ai_mercos_customer_document_index (
    tenant_id       text        NOT NULL,
    document_digest char(64)    NOT NULL,
    customer_id     text        NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- A legacy base may hold the same document on several customers: the key
    -- allows it, and the lookup reports AMBIGUOUS instead of picking one.
    PRIMARY KEY (tenant_id, document_digest, customer_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_mercos_customer_id
    ON public.ai_mercos_customer_document_index (tenant_id, customer_id);

-- Which key built the index, and whether a FULL baseline (a sweep that started
-- from an empty cursor and reached the end) has completed with that key. Only
-- the fingerprint of the key is stored, never the key.
CREATE TABLE IF NOT EXISTS public.ai_mercos_customer_index_config (
    tenant_id             text        PRIMARY KEY,
    key_fingerprint       char(64)    NOT NULL,
    baseline_started_at   timestamptz NOT NULL DEFAULT now(),
    baseline_completed_at timestamptz NULL,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- Durable claim per document: at most one creation per tenant + document,
-- across conversations and workers. Survives until the synced index sees the
-- customer. `unknown` (POST outcome not confirmed) blocks further creation
-- until someone verifies it.
CREATE TABLE IF NOT EXISTS public.ai_mercos_customer_creation_claim (
    tenant_id       text        NOT NULL,
    document_digest char(64)    NOT NULL,
    status          text        NOT NULL CHECK (status IN ('creating', 'created', 'unknown')),
    customer_id     text        NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_digest)
);
