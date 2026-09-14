# OBSERVABILITY_BREAKING_RENAME — Parte 1 (neutralizacao NewStore/Tray)

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
