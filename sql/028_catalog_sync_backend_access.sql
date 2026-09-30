-- Apply the existing catalog cursor schema with backend-only access.
CREATE TABLE IF NOT EXISTS public.ai_commerce_sync_state (
    -- SEM DEFAULT de proposito: uma insercao que esqueca o tenant deve FALHAR,
    -- nunca cair silenciosamente no tenant de outra marca. O runtime sempre
    -- envia COMMERCE_TENANT_ID explicitamente.
    tenant_id             text        NOT NULL,
    provider              text        NOT NULL,
    resource              text        NOT NULL,

    -- Ultima posicao CONFIRMADA (todos os itens da pagina gravados com sucesso).
    last_cursor           text        NULL,
    last_success_at       timestamptz NULL,

    -- Observabilidade da ultima execucao. `last_error_code` guarda o codigo
    -- estruturado (rate_limited, transport_error, ...), nunca payload nem
    -- mensagem que possa carregar dado de cliente.
    last_attempt_at       timestamptz NULL,
    last_error_code       text        NULL,
    consecutive_failures  integer     NOT NULL DEFAULT 0,

    -- Contadores acumulados, uteis para conferir progresso sem ler o indice.
    total_pages           bigint      NOT NULL DEFAULT 0,
    total_records         bigint      NOT NULL DEFAULT 0,

    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, provider, resource)
);

-- Consulta do /health: "qual recurso esta atrasado?".
CREATE INDEX IF NOT EXISTS idx_ai_commerce_sync_state_last_success
    ON public.ai_commerce_sync_state (provider, last_success_at DESC);

ALTER TABLE public.ai_commerce_sync_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.ai_commerce_sync_state FROM anon, authenticated;
