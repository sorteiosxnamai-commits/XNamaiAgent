-- 024_ai_catalog_index_neutral_factual_source_default.sql
--
-- PROPOSTA — NAO EXECUTADA. Nenhum SQL foi rodado ao preparar este arquivo.
-- Aplicacao manual, em janela combinada, depois de revisao.
--
-- Contexto: 017_ai_catalog_index.sql criou `factual_source` com o DEFAULT do
-- fornecedor anterior. O runtime atual SEMPRE grava o valor explicitamente
-- (`commerce_search` via app/commerce/mercos/catalog.py) e o bootstrap em
-- app/db.py ja usa o default neutro — o default antigo so seria usado por um
-- INSERT manual sem a coluna. Esta migration apenas alinha o default.
--
-- Nao altera nenhuma linha existente (isso e a etapa separada descrita em
-- docs/legacy_commerce_values_migration.md). Nao reescreve a 017.
-- Idempotente: pode ser executada mais de uma vez.

BEGIN;

ALTER TABLE IF EXISTS public.ai_catalog_index
    ALTER COLUMN factual_source SET DEFAULT 'commerce_search';

COMMIT;

-- Verificacao (somente leitura):
--   SELECT column_default FROM information_schema.columns
--   WHERE table_schema = 'public' AND table_name = 'ai_catalog_index'
--     AND column_name = 'factual_source';
--
-- Rollback (volta ao default da 017):
--   ALTER TABLE IF EXISTS public.ai_catalog_index
--       ALTER COLUMN factual_source SET DEFAULT 'tray_search';
