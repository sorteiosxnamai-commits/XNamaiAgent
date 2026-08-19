# Etapa 9 — Learning só proposta pendente

## Objetivo

Attendance learning continua gerando **reviews + insights** `pending_review`,
mas **não** promove/ativa instruction extensions sem humano.

## Defaults

| Variável | Antes | Depois |
|---|---|---|
| `AGENT_LEARNING_AUTO_PROMOTE` | true | **false** |
| `AGENT_LEARNING_AUTO_ACTIVATE` | true | **false** |

## Comportamento

1. Cron `run_attendance_learning_batch` — classifica, grava reviews/insights.
2. Com promote **off** (default): para em insight `pending_review`.
3. Com promote **on** + activate **off**: cria extension `pending_review`, linka `applied_extension_id`, insight **permanece** `pending_review` (não marca `applied`).
4. Activate só com `AGENT_LEARNING_AUTO_ACTIVATE=true` (rollback) **ou** admin `POST .../approve`.

Turn-envelope extensions (`memory_service`) já eram pending — inalterado.

## Arquivos

- `app/config.py`, `app/attendance_learning.py`
- `.env.example`
- `tests/test_attendance_learning.py`, `tests/test_config_defaults.py`

## Rollback

```env
AGENT_LEARNING_AUTO_PROMOTE=true
AGENT_LEARNING_AUTO_ACTIVATE=true
```

## Testes

- Defaults false
- Promote sem activate → create, sem `approve_extension`, insight não vira `applied`
- Promote com activate → approve + insight `applied`
- Batch default → zero promotions
