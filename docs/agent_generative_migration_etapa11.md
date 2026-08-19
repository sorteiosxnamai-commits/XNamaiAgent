# Etapa 11 — Replay harness real (não cheat)

## Objetivo

Evals offline honestas: o agente roda de verdade (com fakes); o score usa só
observações da execução — **nunca** copia `expected` → `observed_*`.

## Comando

```bash
pytest -m offline_eval
```

## O que mudou

| Antes | Depois |
|---|---|
| `test_agent_quality_eval` forçava score 100 copiando expected | Só valida **schema** dos fixtures |
| Sem marker `offline_eval` | `pytest.ini` registra o marker |
| 0 cenários de replay | `scenarios_v1.json` com **25** casos |
| — | `tests/evals/harness.py` + `test_offline_replay.py` |

## Arquivos

- `pytest.ini`
- `tests/evals/harness.py`
- `tests/evals/fixtures/scenarios_v1.json`
- `tests/evals/test_offline_replay.py`
- `tests/evals/test_agent_quality_eval.py` (schema only)
- `tests/evals/test_offline_agent_evals.py` (marcado `offline_eval`)
- `tests/evals/scoring.py` (inalterado — já era honesto)

## Como o harness observa

1. `OPENAI_API_KEY=""` + Tray URL vazia
2. `execute_tool` fake a partir de `tool_fixtures`
3. `generate_agent_reply_async`
4. Extrai domain / tools / reply / handoff / openai_calls do resultado
5. `score_eval_case(...)` compara com expected

## Cenários (25)

Greeting, OOS, handoff humano, prompt injection, trade-in, buscas (marca/orçamento/cor/ref/typo/mecanismo), catálogo vazio, tray timeout, horário loja, multi-tenant hint, resume de link de pagamento com `initial_state`.

## Rollback

Não há flag de produto; remover/skip `test_offline_replay` se necessário.
O antigo cheat **não** deve voltar.
