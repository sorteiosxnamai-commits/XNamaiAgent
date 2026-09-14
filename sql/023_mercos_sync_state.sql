-- 023_mercos_sync_state.sql
--
-- PROPOSTA — NAO EXECUTADA. Nenhum SQL foi rodado na Parte 3.
--
-- Estado de sincronizacao incremental por recurso do provider comercial.
-- Auditei a infraestrutura existente antes de propor: nenhuma tabela atual
-- guarda cursor/watermark (as `ai_*` cobrem conversa, catalogo, memoria, inbox e
-- persona). `ai_catalog_index` armazena o ITEM sincronizado, nunca a posicao da
-- sincronizacao. Por isso esta tabela nova, minima.
--
-- Nada aqui altera migration antiga. Nenhum DROP, nenhum ALTER destrutivo.
--
-- Semantica do cursor (contrato do MercosAdaptor):
--   * a listagem devolve `nextCursor` apenas quando existe proxima pagina;
--   * o consumidor so pode gravar o cursor DEPOIS de persistir toda a pagina;
--   * reexecutar a mesma pagina precisa ser idempotente.
-- `last_cursor` guarda a ultima posicao CONFIRMADA. Falha no meio de uma pagina
-- deixa o cursor onde estava e a pagina inteira e reprocessada.

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
