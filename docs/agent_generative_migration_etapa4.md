# Etapa 4 — Busca híbrida de relógios

## Diagnóstico

Retrieval era Tray-probe + hard_filter parcial + rerank LLM (máx. 5 IDs) + revalidate top-3.
Não havia `ProductCandidate`, índice canônico, nem hard/soft explícitos do `TurnUnderstanding`.

## Implementação

| Peça | Onde |
|------|------|
| `CanonicalCatalogItem`, `ProductCandidate` | `app/catalog_index.py` |
| Hybrid rank (exact → lexical → alias → trigram → soft) | `hybrid_rank_candidates` |
| Hard constraints (exclusive / somente / budget) | `hard_filter_products` + `evaluate_hard_constraints` |
| Rerank 5–20 IDs, rejeita inventados, log prior/posterior | `rerank_products` |
| Revalidate top-N live Tray | `revalidate_products` + `AGENT_REVALIDATE_TOP_N` |
| Índice durável (tenant, product_id, …) | `sql/017_ai_catalog_index.sql` + `ensure_tables` |
| Wire recommendation path | `sales_agent._execute_compiled_product_retrieval` |

## Prioridade atual (sobre pool já buscado)

1. Filtros hard (EAN/SKU/ref/marca exclusiva/orçamento/somente)
2. Score soft (lexical, aliases de cor, gênero, trigram)
3. Rerank LLM só sobre candidatos reais (≤20)
4. Revalidação Tray só do top-N exibido

BM25/pg_trgm full-corpus e embeddings de catálogo ficam para evolução (índice já materializa linhas).

## Config

```env
AGENT_CANDIDATE_POOL_LIMIT=20
AGENT_RERANK_SELECTION_LIMIT=15
AGENT_REVALIDATE_TOP_N=3
AGENT_CATALOG_INDEX_WRITE_ENABLED=true
```
