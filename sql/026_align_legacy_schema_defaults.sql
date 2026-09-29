-- 026_align_legacy_schema_defaults.sql
--
-- PROPOSTA — NAO EXECUTADA. Aplicacao manual, em janela combinada, depois de
-- revisao. Idempotente: pode ser executada mais de uma vez.
--
-- Por que existe: duas migrations foram editadas depois de publicadas.
--
-- * 017_ai_catalog_index.sql foi reescrita em e34420e (default de tenant e
--   cinco colunas de atributos de relogio do dominio anterior). Instalacoes
--   que rodaram a versao original ficariam com um schema diferente das novas.
--   A 017 foi RESTAURADA ao conteudo original publicado; esta migration leva as
--   duas instalacoes ao mesmo schema final (o mesmo que app/db.py cria).
-- * 023_mercos_sync_state.sql teve o DEFAULT de tenant removido em f9e3fd6
--   (ainda como proposta nao executada). Se alguma instalacao chegou a rodar a
--   primeira versao, o DROP DEFAULT abaixo a alinha; nas demais e no-op.
--
-- Colunas removidas: nenhum codigo do agente le ou grava essas colunas (o
-- catalogo Mercos nao tem esses atributos). Antes de aplicar em producao,
-- confira se ha algum valor nelas (consulta de verificacao ao final).

BEGIN;

ALTER TABLE IF EXISTS public.ai_catalog_index
    ALTER COLUMN tenant_id SET DEFAULT 'xnamai';

ALTER TABLE IF EXISTS public.ai_catalog_index
    DROP COLUMN IF EXISTS mechanism,
    DROP COLUMN IF EXISTS case_size,
    DROP COLUMN IF EXISTS dial_color,
    DROP COLUMN IF EXISTS strap_color,
    DROP COLUMN IF EXISTS strap_type;

ALTER TABLE IF EXISTS public.ai_commerce_sync_state
    ALTER COLUMN tenant_id DROP DEFAULT;

COMMIT;

-- Verificacao ANTES de aplicar (somente leitura; esperado: 0):
--   SELECT count(*) FROM public.ai_catalog_index
--    WHERE mechanism IS NOT NULL OR case_size IS NOT NULL OR dial_color IS NOT NULL
--       OR strap_color IS NOT NULL OR strap_type IS NOT NULL;
--
-- Rollback: as colunas removidas voltam com os tipos da 017 original
-- (`text NULL`) e o default de tenant volta ao valor registrado na 017.
