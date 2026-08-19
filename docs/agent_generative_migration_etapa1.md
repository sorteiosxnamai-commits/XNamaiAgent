# Etapa 1 — Diagnóstico: fluxo atual → agente generativo factual

**Projeto:** NSAgentForSorteios  
**Baseline:** 847 testes coletados; `openai==2.7.2`  
**Data:** 2026-08-05  
**Escopo:** documento de mapeamento (sem mudança de comportamento nesta etapa)

---

## 1. Fluxo atual (entrada → resposta)

```text
Brevo webhook
  → verify_brevo_webhook
  → parse / inbound_skip_reason / event_skip
  → dedupe (message_id) + conversation_lock + caption coalesce
  → claim_inbound_message
  → human_takeover_active? (ChatBô) → skip reply
  → process_incoming_message
       → load commerce state + identity
       → generate_agent_reply_async
            → guardrails / handoff keywords / trade-in
            → history (hard_cap=80 recovery, model window=12)
            → interpret_message (structured SalesInterpretation)
            → handle_sales_message | raffle | greeting | fallback
            → product_retrieval + Tray tools (+ catalog_cache)
            → optional GPT wording (responder)
       → evaluate_policy (shadow)
       → factual_validation (SHADOW default)
       → response_critique (ENFORCE default) ± regenerate
       → compose + present
  → send_brevo_reply
  → insert_agent_response + memory proposals + remarketing sync
```

Arquivos-chave: `api/index.py`, `app/message_pipeline.py`, `app/openai_agent.py`, `app/sales_agent.py`, `app/openai_gateway.py`, `app/product_retrieval.py`, `app/factual_validator.py`, `app/response_critique.py`, `app/brevo_client.py`.

---

## 2. Mapa por etapa operacional

| Etapa | Onde | Observação |
|-------|------|------------|
| Entrada | `api/index.py` `handle_brevo_conversations_webhook` | WA + conversations no mesmo handler |
| Normalização | `webhook_parser.py` | canal, sender_key, visitor, image/caption |
| Coalescência | `inbound_coalesce.py`, lock, claim | caption echo 60s; lock 15s |
| Conversa | `db.load_recent_conversation_turns`, `history_window.py` | recovery ≠ janela do modelo |
| Persona | `prompt_compiler.py` + DB persona | interpreter usa prompt fixo separado |
| Memória | `contact_memory_*`, `memory_service` | auto-apply off; prompt injection opcional |
| Interpretação | `sales_agent.interpret_message` | `SalesInterpretation` (não TurnUnderstanding unificado) |
| Busca | `product_retrieval.py`, `catalog_cache.py` | aliases de cor; hard vs soft prefs parcial |
| OpenAI | `openai_gateway.py` | **default Chat Completions**; Responses já implementado |
| Composição | `sales_agent` responder + `response_composer` | |
| Presenter | `response_presenter.py` | regex pode cortar perguntas/CTAs legítimas |
| Crítica | `response_critique.py` | enforce; regenera **após** factual |
| Fatos | `factual_validator.py`, `fact_sources.py` | **shadow** — não bloqueia em prod default |
| Persistência | `ai_inbound_messages`, `ai_agent_responses`, commerce sessions | |
| Envio | `brevo_client.send_brevo_reply` | dry_run possível; imagem = link |
| Métricas | `turn_metrics.py`, `observability.py` | full obs default agressivo |
| Humano | `human_takeover.py` + `handoff_service.py` | ChatBô mute ≠ Brevo `mark_for_human` (só metadata) |

---

## 3. Estado OpenAI / config relevante

| Item | Valor atual |
|------|-------------|
| Pacote | `openai==2.7.2` |
| `OPENAI_API_MODE` | `chat_completions` |
| `OPENAI_RESPONSES_TRAFFIC_PERCENT` | `0.0` |
| `OPENAI_STORE_RESPONSES` | `false` |
| `OPENAI_USE_PREVIOUS_RESPONSE_ID` | `false` (flag existe; **nunca anexado** no gateway) |
| `AGENT_MAX_LLM_CALLS_PER_TURN` | `6` |
| `AGENT_FACTUAL_VALIDATION_MODE` | `shadow` |
| `AGENT_CRITIQUE_MODE` | `enforce` |
| `AGENT_QUALITY_JUDGE_MODE` | `shadow` (inativo se critique ≠ off) |
| `AGENT_LEARNING_AUTO_PROMOTE/ACTIVATE` | `true` (risco) |

---

## 4. Problemas confirmados no código

1. **Responses não é caminho principal** — Chat Completions default; canary em 0%.
2. **Orçamento LLM alto (6)** — interpret + respond + critique + regenerate facilmente estoura meta de 1–2 calls.
3. **Factual em shadow** — inventário/preço/URL ruins podem sair; critique regenera **sem** revalidar fatos.
4. **Compreensão fragmentada** — `SalesInterpretation` ≠ hard/soft constraints unificados; IDs não passam por contrato `TurnUnderstanding`.
5. **Catálogo** — recuperação híbrida parcial; sem índice canônico completo (SKU/EAN/cores normalizadas/TTL factual por campo).
6. **Presenter excessivo** — pode remover perguntas/CTAs válidas por regex.
7. **Evals desonestas** — `tests/evals/test_agent_quality_eval.py` monta `observed_*` a partir do `expected` e força score 100.
8. **Attendance learning** — promove e **ativa** instruction extensions sem aprovação humana.
9. **Handoff Brevo** — `mark_for_human` não muda estado no provedor; só texto + metadata.
10. **Persona vs interpreter** — persona DB não governa a interpretação de domínio/intent.

---

## 5. Fluxo proposto (alvo)

```text
entrada/dedupe/lock (inalterado)
  → TurnUnderstanding (1× structured Responses)  [ou 0× se determinístico]
  → ferramentas determinísticas (Tray/DB) + filtros hard / score soft
  → revalidação factual só dos top-N
  → 1× resposta grounded (Responses, store=false, sem previous_response_id)
  → validação determinística ENFORCE
  → critique/judge só amostragem / baixo confidence / alto impacto (shadow→off)
  → presenter mínimo (canal/URL/blocks)
  → persistência + métricas
```

Estado comercial e histórico continuam **só no Postgres da aplicação**.

---

## 6. Plano de fases (compatível)

| Fase | Objetivo | Critério de saída |
|------|----------|-------------------|
| **1** | Este diagnóstico | Doc aprovado |
| **2** | Responses = primary configurável + contratos gateway + testes; sem upgrade cego do SDK | Suíte verde; shadow/canary ok |
| **3** | `TurnUnderstanding` + adapter compatível com `SalesInterpretation` | Interpret path dual / feature flag |
| **4** | Índice canônico + retrieval híbrido + revalidate top-N | Evals offline de catálogo |
| **5** | Autoridades factuais separadas | Factual enforce nos paths comerciais |
| **6** | Orçamento LLM ↓; critique shadow; judge gated | p50 calls/turn ≤ 2 em smoke |
| **7** | Hierarquia de prompt + presenter leve | Diff de estilo + testes |
| **8** | Resumo incremental + memória segura | Policy tests |
| **9** | Learning só proposta pendente | Auto-activate = false |
| **10** | Métricas + obs default off | Sem PII em defaults |
| **11** | Replay harness real (não cheat) | `pytest -m offline_eval` |
| **12** | Rollout 5→100% + rollback | Canary + alertas |

---

## 7. Riscos e compatibilidade

- **Não** remover Chat Completions até fallback medido.
- **Não** alterar contratos Brevo/admin sem camada de compat.
- Upgrade `openai` só após testes de contrato do gateway na versão atual.
- Dual-run: `SalesInterpretation` ↔ `TurnUnderstanding` via adapter até cutover.
- Migrations novas idempotentes; tenant_id em todo índice de catálogo.

---

## 8. Testes que validam a Etapa 1

Nenhuma mudança de código de produto nesta etapa. Baseline:

```bash
python -m pytest --collect-only -q   # 847 collected
```

Próxima fase (2) começará com testes de contrato do gateway **antes** de qualquer bump de `openai`.

---

## 9. Decisão pedida antes da Etapa 2

~~Confirmar para iniciar a Etapa 2~~ — **aprovada e concluída** (ver `docs/agent_generative_migration_etapa2.md`).
