# Plano: migrar valores comerciais legados e remover a compatibilidade

Estado atual (garantido por `tests/test_legacy_commerce_values_read_only.py`):

| Valor legado | Onde pode existir gravado | Valor novo | Leitura compatível |
| --- | --- | --- | --- |
| `tray_live` | `ai_catalog_index.factual_source`, `_factual_source` em payloads | `commerce_live` | `LIVE_PROVIDER_SOURCE_VALUES` (`app/fact_sources.py`) |
| `tray_search` | `ai_catalog_index.factual_source` (e o DEFAULT da 017) | `commerce_search` | `SEARCH_PROVIDER_SOURCE_VALUES`, `FactualSource` (`app/catalog_index.py`) |
| `tray_api` | `product_evidence.source` em metadados de resposta de Story | `commerce_api` | `LIVE_EVIDENCE_SOURCES` (`app/story_commercial_policy.py`) |

Escrita nova usa **somente** os nomes `commerce_*`. Nenhuma migration antiga é
reescrita.

## Etapa 1 — default (preparada, não aplicada)

`sql/024_ai_catalog_index_neutral_factual_source_default.sql`: troca apenas o
DEFAULT da coluna. Não toca em linhas.

## Etapa 2 — dados históricos (plano; SQL NÃO fornecido como migration)

Executar só depois da etapa 1, em janela combinada, com backup:

1. **Medir** (somente leitura):
   ```sql
   SELECT factual_source, count(*) FROM public.ai_catalog_index GROUP BY 1;
   ```
2. **Atualizar em lotes** pequenos, por tenant, para não travar a tabela
   (`UPDATE ... SET factual_source = 'commerce_search' WHERE factual_source =
   'tray_search' AND ctid IN (SELECT ctid ... LIMIT 5000)`), repetindo até zerar;
   idem `tray_live` -> `commerce_live`.
3. `product_evidence.source = 'tray_api'` vive dentro de JSON de metadados de
   resposta (auditoria). **Não reescrever auditoria**: esses registros ficam
   como histórico; o runtime não os relê para decidir preço/estoque.
4. **Verificar** que a contagem de valores legados é zero em `ai_catalog_index`.

## Etapa 3 — remover a compatibilidade de leitura

Somente com a etapa 2 verificada **em todos os ambientes**:

- tirar `tray_live`/`tray_search` de `LIVE/SEARCH_PROVIDER_SOURCE_VALUES` e do
  `FactualSource`;
- manter `tray_api` em `ProductEvidence` enquanto houver leitura de auditoria
  antiga que valide o modelo; caso contrário, remover também;
- atualizar `READ_COMPAT_DEFINITIONS` e o `ALLOWLIST` de
  `tests/test_legacy_residue_gate.py` (o gate falha se uma exceção ficar
  obsoleta, forçando a limpeza).

Rollback de cada etapa: a etapa 1 tem rollback no próprio arquivo; a etapa 2 é
reversível pelo backup; a etapa 3 é um revert de código.
