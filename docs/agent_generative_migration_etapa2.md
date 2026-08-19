# Etapa 2 — Responses API como caminho principal

**SDK:** `openai==2.7.2` (sem bump — contratos cobrem Responses create/parse/tools/usage).  
**Modelos recomendados (sem benchmark online nesta fase):**
- **main:** `gpt-4.1-mini` — compreensão + resposta grounded (custo/qualidade equilibrados)
- **fast:** `gpt-4.1-nano` — tarefas estruturadas simples (interpretadores futuros)

## Defaults

| Variável | Valor |
|----------|--------|
| `OPENAI_API_MODE` | `responses` |
| `OPENAI_RESPONSES_TRAFFIC_PERCENT` | `1.0` |
| `OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED` | `false` |
| `OPENAI_STORE_RESPONSES` | `false` |
| `OPENAI_USE_PREVIOUS_RESPONSE_ID` | `false` (nunca anexado) |
| `OPENAI_RESPONSES_FALLBACK_TO_CHAT` | `true` |

Rollout gradual em produção: `OPENAI_API_MODE=canary` + percent 0.05→1.0.

## O que mudou

- `ResponsesGateway`: instructions/input, structured, tools, reasoning.effort, text.verbosity, max_output_tokens, timeout default, métricas (tokens in/out/cache/reasoning, status, call_id)
- Tool loop preserva function_call + function_call_output com call_id
- Cliente AsyncOpenAI com `timeout` + `max_retries` de config
- `resolve_openai_model(main|fast)` em `app/openai_models.py`
