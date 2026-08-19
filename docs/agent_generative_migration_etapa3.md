# Etapa 3 — TurnUnderstanding unificado

## Objetivo

Substituir interpretações semânticas fragmentadas por um contrato único, mantendo
`SalesInterpretation` como fachada de compatibilidade para retrieval/checkout.

## Diagnóstico

- `interpret_message` emitia só `SalesInterpretation` (domínio/goal misturados com ações).
- Hard vs soft preferences não eram explícitos (tudo em `preferences`).
- Clarificação dependia sobretudo do modelo (`needs_clarification`).
- IDs internos podiam ser inventados sem sanitização.

## Implementação

| Peça | Arquivo |
|------|---------|
| Contrato + adapters + policy | `app/turn_understanding.py` |
| Flag | `AGENT_TURN_UNDERSTANDING_ENABLED` (default `true`) |
| Wire | `app/sales_agent.interpret_message` |
| PrivateAttr | `SalesInterpretation._turn_understanding` |

Fluxo com flag on:

1. Structured output → `TurnUnderstanding` (modelo fast)
2. `sanitize_turn_understanding` (zera claimed IDs)
3. `apply_clarification_policy` (Casio≤500 → busca; “quero esse” sem ref → clarifica)
4. `turn_understanding_to_sales` → pipeline existente
5. `normalize_sales_interpretation`

Flag off: schema legado + `sales_to_turn_understanding` em shadow no private attr.

## Decisões

- Modelo de interpretação: `OPENAI_FAST_MODEL` (default `gpt-4.1-nano`)
- Schema strict (sem `default` no JSON Schema) para Responses/Chat parse
- IDs internos nunca confiáveis do LLM
- Downstream ainda usa `SalesInterpretation` (sem breaking change)

## Testes

`tests/test_turn_understanding.py` + legado com flag off em `test_sales_interpreter_request.py`
