# OBSERVABILITY_BREAKING_RENAME — Parte 1 (neutralizacao XNamai/Tray)

A telemetria comercial deixou de citar o fornecedor legado no nome dos eventos,
contadores e campos.

| Nome antigo | Nome novo |
| --- | --- |
| `tray.call` (evento) | `commerce.call` |
| `tray_call_count` | `commerce_call_count` |
| `tray_latency_ms` | `commerce_latency_ms` |
| `tray_tools` (snapshot do turno) | `commerce_tools` |
| `used_tray` (response_metadata) | `used_commerce_provider` |
| `record_tray_observation()` | `record_commerce_observation()` |
| `summarize_tray_result()` | `summarize_commerce_result()` |

## Por que sem camada de compatibilidade

Nenhum consumidor dos nomes antigos existe dentro deste repositorio — verificado
em `app/`, `api/`, `scripts/`, `tests/`, `.github/` e `vercel.json`. Emitir os
dois nomes em paralelo so duplicaria volume de log sem leitor.

A guarda `tests/test_commerce_telemetry_naming.py` falha se qualquer simbolo com
nome de fornecedor voltar aos modulos de runtime — inclusive em comentario, que
foi o motivo de esta nota morar aqui e nao dentro de `app/observability.py`.

## O que quebra fora do repositorio

Dashboards, alertas e queries montados sobre os nomes antigos **param de casar**
e precisam ser atualizados junto com o deploy desta mudanca. Nenhuma migracao
automatica e possivel: os eventos antigos ja emitidos permanecem com o nome
antigo no destino de logs.

## Fora desta renomeacao

`_factual_source` (`tray_live` / `tray_search`) NAO foi renomeado a esmo: esses
valores estao **gravados** na tabela `ai_catalog_index`. A escrita passou a usar
`commerce_live` / `commerce_search`, e a leitura aceita os dois nomes
(`LIVE_PROVIDER_SOURCE_VALUES` / `SEARCH_PROVIDER_SOURCE_VALUES` em
`app/fact_sources.py`). Nenhum SQL de migracao foi executado.

## Parte 2 — valores e logs ainda produzidos com o nome legado

Mesma regra: ninguem no repositorio le os nomes antigos; dashboards e queries
externos precisam acompanhar o deploy.

| Onde | Nome antigo | Nome novo |
| --- | --- | --- |
| log `app/sales_agent.py` | `[sales.agent] tray_request` / `tray_result` | `commerce_request` / `commerce_result` |
| log `app/story_product_matcher.py` | `[story.matcher.tray.error]` | `[story.matcher.provider.error]` |
| log `app/product_reference_resolver.py` | `product_reference_tray_error` | `product_reference_provider_error` |
| razao de candidato do Story | `tray_query_overlap:*` | `provider_query_overlap:*` |
| `response_metadata` da busca por imagem | `tray_count` | `provider_count` |
| codigo de falha da revalidacao do Story | `tray_unavailable` | `commerce_provider_unavailable` |
| `ai_catalog_cache.metadata.source` | `tray_brand_or_category` | `commerce_brand_or_category` |
| `ai_conversation_statuses.completion_reason` | `payment_confirmed_by_tray` | `payment_confirmed_by_commerce` |
| `ProductSnapshot.source` | `tray_adapter` | `commerce_provider` |
| `ProductEvidence.source` (Story) | `tray_api` | `commerce_api` |

Leitura temporaria de valores ja gravados: `ProductEvidence.source` continua
aceitando `tray_api` (`LIVE_EVIDENCE_SOURCES` em
`app/story_commercial_policy.py`). Linhas antigas de `ai_catalog_cache` e
`ai_conversation_statuses` mantem o texto antigo; nenhum leitor depende dele.
