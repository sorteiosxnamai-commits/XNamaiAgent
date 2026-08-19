# Etapa 7 — Hierarquia de prompt + presenter leve

## Objetivo

Respostas mais naturais sem cirurgia regex agressiva pós-LLM:

- Presenter mínimo: similar-product, blocos de canal, preservar URLs.
- Estilo (1 pergunta / sem “Claro”) vive no **prompt**, não no post-process.
- Dual-run: `full` | `thin` | `shadow`.

## Defaults

| Variável | Valor |
|---|---|
| `AGENT_PRESENTER_MODE` | **thin** |

Rollback: `AGENT_PRESENTER_MODE=full`.

Soak sem mudar outbound: `AGENT_PRESENTER_MODE=shadow` (cliente recebe full; metadata com `thin_preview` + `diff`).

## Modos do presenter

| Modo | Outbound | Comportamento |
|---|---|---|
| `thin` | thin | similar + blocks + URLs; handoff/OOS → 0 perguntas |
| `full` | full | legacy: opener/CTA/emoji/limit questions |
| `shadow` | full | calcula thin; log `[agent.presenter.shadow]` + metadata.diff |

Thin **não** remove segunda pergunta comercial legítima (problema da Etapa 1).

## Hierarquia de prompt

Documentada em `app/prompt_layers.py`:

1. `fixed_safety_policy`
2. `user_managed_persona`
3. `approved_instruction_extensions`
4. `channel_overlay`
5. `customer_memory`
6. `operational_contract`

`STYLE_VOICE_RULES` é a fonte única usada por `channel_system_hint`.  
`SALES_RESPONDER_INSTRUCTIONS` inclui grounding de estilo alinhado (não triplicar no presenter).

## Arquivos

- `app/response_presenter.py` — thin/full/shadow
- `app/prompt_layers.py` — ordem + style voice
- `app/channel_profiles.py` — hint via STYLE_VOICE_RULES
- `app/sales_agent.py` — estilo no responder
- `app/config.py`, `.env.example`
- `tests/test_response_presenter.py`

## Riscos

- Thin deixa passar “Claro!” / multi-CTA se o modelo ignorar o prompt → monitorar shadow diffs antes de confiar só em thin em canais novos.
- Handoff continua com rail de 0 perguntas mesmo em thin.

## Testes

- full: strip opener + ≤1 pergunta
- thin: preserva 2 perguntas commerce; handoff 0
- shadow: outbound full + thin_preview/diff
- STYLE_VOICE_RULES no channel hint
- default mode = thin
