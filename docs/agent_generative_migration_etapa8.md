# Etapa 8 — Resumo incremental + memória segura

## Objetivo

Continuidade de conversa sem virar segunda fonte de fatos comerciais:

- Summary só quando há progresso real (critérios existentes).
- Sanitize antes de persistir (PII / URL / preço-estoque-frete-pedido).
- Inject no prompt opcional e **não-autoritativo**.
- Contact memory: rejeitar fatos comerciais voláteis; auto-apply continua **off**.

## Defaults (seguros)

| Variável | Default |
|---|---|
| `AGENT_CONVERSATION_SUMMARY_ENABLED` | **false** |
| `AGENT_CONVERSATION_SUMMARY_IN_PROMPT_ENABLED` | **false** |
| `AGENT_MEMORY_AUTO_APPLY_ENABLED` | **false** |
| `AGENT_MEMORY_PROPOSALS_ENABLED` | true |
| `AGENT_CONTACT_MEMORY_IN_PROMPT_ENABLED` | true |

Soak sugerido:
1. `AGENT_CONVERSATION_SUMMARY_ENABLED=true` (só write + scrub)
2. depois `AGENT_CONVERSATION_SUMMARY_IN_PROMPT_ENABLED=true`

## Policy

`evaluate_summary_delta` = sanitize → criteria.

Rejeita / remove campos com:
- sensitive (CVV, cartão, senha, CPF, …)
- prompt injection
- URL
- commercial volatile (`R$`, estoque, frete, status/pedido #, pagamento.php)

Contact memory (`evaluate_memory_proposal`) ganha o mesmo `commercial_volatile`
(exceto `price_preference` / `preferred_price_*`).

## Prompt

Camada `conversation_summary` em `PROMPT_LAYER_ORDER` (após `customer_memory`).
Bloco inclui disclaimer: não usar como fonte de preço/estoque/URL/pedido.

## Wiring

- `memory_service`: summary/extensions rodam mesmo se proposals off
- `sales_agent` / `message_pipeline`: envelope quando summary enabled
- `prompt_compiler`: inject se `*_IN_PROMPT_ENABLED`

## Arquivos

- `app/conversation_summary_policy.py`
- `app/memory_policy.py`, `app/memory_service.py`
- `app/prompt_compiler.py`, `app/prompt_layers.py`
- `app/sales_agent.py`, `app/message_pipeline.py`, `app/config.py`
- `tests/test_conversation_summary_policy.py`
- `tests/test_conversation_summary_prompt.py`
- `tests/test_memory_policy.py`

## Riscos

- Summary enabled sem scrub antigo já no DB → inject só lê o que já está; re-soak limpa.
- Working memory ainda pode carregar `payment_url` (fora do escopo desta etapa).

## Rollback

Manter ambos summary flags `false` (default).
