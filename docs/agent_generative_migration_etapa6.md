# Etapa 6 — Reduzir chamadas LLM por turno

## Objetivo

Cortes de custo/latência sem abrir mão da autoridade factual determinística:

- Turnos normais: ≤1–2 chamadas OpenAI (interpret + respond).
- Critique/judge LLM só com risco, falha factual, ação de alto impacto, seed determinístico, amostragem controlada ou eval offline.
- Nunca usar segundo LLM no lugar da validação factual.

## Defaults

| Variável | Antes | Depois |
|---|---|---|
| `AGENT_MAX_LLM_CALLS_PER_TURN` | 6 | **3** |
| `AGENT_MAX_LLM_CALLS_PER_TURN_COMPLEX` | — | **5** |
| `AGENT_CRITIQUE_MODE` | enforce | **shadow** |
| `AGENT_CRITIQUE_LLM_ON_RISK_ONLY` | — | **true** |
| `AGENT_CRITIQUE_SHADOW_SAMPLE_RATE` | — | **0** |
| `AGENT_QUALITY_JUDGE_MODE` | shadow | **off** |
| `AGENT_QUALITY_JUDGE_SAMPLE_RATE` | — | **0** |
| `AGENT_FACTUAL_VALIDATION_MODE` | enforce | enforce (inalterado) |

## Fluxo

1. Fast deterministic critique (sem LLM) — trade-in, greeting dedupe, preferências mal interpretadas.
2. Gate `should_run_llm_critique` (`app/llm_call_policy.py`) usando sinais de `collect_judge_risk_signals`.
3. Se skip → metadata `risk_gate` / `skip_reason`; resposta segue.
4. Se seed determinístico fail → força LLM (enforce regenera; shadow só observa).
5. Pós-critique: factual validation de novo (Etapa 5).
6. Quality judge: default off; se ligado e critique off, só dispara com o mesmo gate.

## Budget vs fallback Responses→Chat

Tentativa Responses que falha e cai em Chat Completions **reembolsa** o slot (`release_failed_openai_attempt`), para o orçamento contar operações lógicas (interpret/respond), não retries de transporte.

## Sinais que disparam LLM critique

- `factual_validation_failed`
- `high_risk_score`
- `low_interpretation_confidence` (agora gravado em `response_metadata`)
- `side_effect_action` / order / payment link
- evidência conflictante / tool failure / fallback parcial

Preço/estoque sozinhos **não** disparam critique LLM (factual enforce cobre).

## Arquivos

- `app/llm_call_policy.py` (novo)
- `app/response_critique.py` — gate após fast path
- `app/message_pipeline.py` — passa risk/factual; judge gated
- `app/config.py`, `.env.example`, `api/index.py`
- `app/openai_agent.py` — `interpretation_confidence`
- `tests/test_llm_call_policy.py`, `tests/test_response_critique.py`, `tests/test_config_defaults.py`

## Rollback

```env
AGENT_MAX_LLM_CALLS_PER_TURN=6
AGENT_CRITIQUE_MODE=enforce
AGENT_CRITIQUE_LLM_ON_RISK_ONLY=false
AGENT_QUALITY_JUDGE_MODE=shadow
```

## Testes

- Gate skip em browse low-risk
- Gate fire em factual fail
- Loop LLM legado com `AGENT_CRITIQUE_LLM_ON_RISK_ONLY=false`
- Defaults Etapa 6
