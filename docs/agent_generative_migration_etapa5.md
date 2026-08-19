# Etapa 5 — Autoridade factual

## Diagnóstico

- `FACT_SOURCE_RANK` colocava `LOCAL_DATABASE` acima de Tray.
- Persona/memory não eram barrados em conflitos comerciais.
- Validação factual rodava **antes** da crítica; regeneração podia sair sem recheck.
- Default `AGENT_FACTUAL_VALIDATION_MODE=shadow`.
- Evidências sem tenant / revalidation status / freshness útil.

## Implementação

| Peça | Arquivo |
|------|---------|
| `PolicyAuthority`, `CommerceDataAuthority`, `ConversationStateAuthority`, `PersonaAuthority` | `app/fact_authority.py` |
| `CommercialClaim` (source, freshness, tenant, product_id, confidence, revalidation) | idem |
| Rank: Security > Deterministic > Tray live > Tray > Local DB > Snapshot > State > Persona > Memory | `app/fact_sources.py` |
| Evidência com `_revalidated` / `_factual_source` → `TRAY_LIVE` | `factual_validator.py` |
| Stock inventado = risco **high** (enforce bloqueia) | idem |
| Factual **após** critique | `message_pipeline.py` |
| Default `enforce` | `config.py` / `.env.example` |

## Prioridade comercial

1. Tray live (revalidado)
2. Tray adapter (busca do turno)
3. Local DB / índice (TTL)
4. Snapshot / cache
5. Conversation state (referência, não preço absoluto)
6. **Nunca** persona / memória para preço, estoque, URL
