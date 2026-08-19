# Etapa 12 — Rollout 5→100% + rollback (canary + alertas)

## Objetivo

Um knob de canary progressivo (`AGENT_ROLLOUT_PROFILE`), kill switch de emergência
e alertas in-process sobre fallback / factual inválido / handoff — sem mutar
tráfego via API (só via env na Vercel).

## Perfis

| Profile | Efeito |
|---|---|
| `full` | Usa `OPENAI_API_MODE` / traffic configurados (default pós-Etapas) |
| `canary_5` | Força mode=`canary`, traffic=**5%** sticky |
| `canary_25` | traffic=**25%** |
| `canary_50` | traffic=**50%** |
| `canary_100` | canary @ **100%** Responses (soak antes de `full`) |
| `emergency` | traffic=0%, TurnUnderstanding off, presenter=`full`, critique/judge=`off` |

`AGENT_EMERGENCY_ROLLBACK=true` força `emergency` independente do profile.

**Factual validation permanece `enforce`** no emergency — não se desliga segurança comercial.

## Sequência sugerida (Vercel)

```text
AGENT_ROLLOUT_PROFILE=canary_5   → soak + logs [openai.canary.turn] / [rollout.alert]
AGENT_ROLLOUT_PROFILE=canary_25
AGENT_ROLLOUT_PROFILE=canary_50
AGENT_ROLLOUT_PROFILE=canary_100
AGENT_ROLLOUT_PROFILE=full       → OPENAI_API_MODE=responses
```

Rollback imediato:

```env
AGENT_EMERGENCY_ROLLBACK=true
```

Checklist completo: `GET /api/admin/rollout` (admin token) ou `/api/health` → `rollout`.

## Alertas

Após cada `turn.quality`, `observe_turn_for_rollout_alerts` mantém janela deslizante:

| Sinal | Default threshold |
|---|---|
| fallback rate | 25% |
| factual_invalid | 10% |
| handoff | 40% |

Emite `[rollout.alert]` (sem PII). Defaults: window=40, min_samples=20.

## Arquivos

- `app/rollout.py` (novo)
- `app/config.py`, `.env.example`
- `app/openai_routing.py`, `app/openai_gateway.py`
- `app/sales_agent.py`, `app/response_presenter.py`, `app/response_critique.py`
- `app/message_pipeline.py`, `api/index.py`
- `tests/test_rollout.py`

## Critério de saída

- Canary 5→100% via um env
- Emergency rollback sem redeploy de código
- Alertas observáveis em log
- Suíte verde
