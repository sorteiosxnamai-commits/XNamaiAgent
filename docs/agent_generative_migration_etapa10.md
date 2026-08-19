# Etapa 10 — Métricas + obs default off (Sem PII em defaults)

## Objetivo

Logs de produção magros por padrão: sem dump de prompts/histórico/HTTP global,
com redaction de PII nos previews que ainda existem.

## Defaults

| Variável | Antes | Depois |
|---|---|---|
| `AGENT_FULL_OBS_LOGS` | true | **false** |
| `AGENT_HTTP_OBS_LOGS` | true | **false** |
| `AGENT_DEBUG_STORE_COMPILED_PROMPT` | false | false (inalterado) |

`full_obs_enabled()` falha **fechado** (False) se config quebrar.

## Comportamento com full_obs off

- Sem evento `openai.prompt`
- `openai.call` sem lista de messages / response_preview
- History turns: só `role`/`chars` (sem `preview`)
- Brevo send: sem `reply_preview`
- Webhook `text_preview` / `reply_preview` passam por `redact_text` (curtos)

`turn.quality` continua com `conversation_key_hash` (sem telefone cru).

## Opt-in debug (Vercel)

```env
AGENT_FULL_OBS_LOGS=true
AGENT_HTTP_OBS_LOGS=true
AGENT_OBS_HASH_SECRET=<salt estável>
```

## Arquivos

- `app/config.py`, `app/observability.py`, `api/index.py`
- `.env.example`
- `tests/test_observability.py`, `tests/test_config_defaults.py`

## Rollback

Ligar as duas flags `true` no ambiente para Runtime Logs detalhados.
