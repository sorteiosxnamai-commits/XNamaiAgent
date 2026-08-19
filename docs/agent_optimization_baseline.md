# Agent optimization baseline

Date: 2026-08-04  
Repo: `NSAgentForSorteios`  
Command: `python -m pytest -q --tb=line`  
Initial result: **679 passed, 5 failed, ~9.01s**  
After baseline fixes + Phase 2 prompt dedup: **687 passed, 0 failed, ~4.6s**

No secrets, phones, documents, or full conversation content are included.

---

## 1. Suite baseline

| Metric | Value |
|--------|-------|
| Total collected (approx) | 684 |
| Passed | 679 |
| Failed | 5 |
| Duration | ~9.01s |

### Failures at baseline

| Test | Root cause class | Action |
|------|------------------|--------|
| `test_invalid_field_does_not_discard_valid_checkout_updates_or_email` | Renamed | Now `test_full_state_name_is_normalized_without_discarding_checkout_data` — **passes** |
| `test_specific_product_keeps_exact_strategy_without_category` | Outdated assertion | Production prepends `token_and_search`; exact strategy still present |
| `test_product_search_uses_progressive_strategies` | Outdated assertion | Production runs parallel multi-strategy probes |
| `test_agent_commerce_calls_tray_before_openai` | Outdated assertion | Tray-before-OpenAI still true; first args are token search |
| `test_interpreter_request_uses_gpt_4_1_mini_and_normalized_messages` | Outdated assertion | Capability catalog appended to interpreter system block |
| `test_health_exposes_only_tray_flags` | **Regression** | Health calls `resolved_mp_access_token()` on mock settings without method |

---

## 2. Production message path (main)

```text
Brevo webhook (api/index.py)
  → parse / skip / channel allowlist
  → inbound_message_exists / claim_inbound_message
  → conversation lock (optional)
  → find_customer_profile_by_phone
  → process_incoming_message (message_pipeline.py)
       → enrich prefs / audio prep (Whisper if audio)
       → load_commerce_conversation_state
       → build_working_memory
       → generate_agent_reply_async (openai_agent.py)
            → load_recent_conversation_turns (limit/hard_cap)
            → deterministic shortcuts (greeting / payment resume / image / order)
            → interpret_message (sales_agent) [OpenAI structured]
            → raffle local | handle_sales_message | legacy tool-loop
                 → Tray retrieval / checkout / PIX / payment
                 → responder composition [OpenAI]
       → evolve + persist commerce state / identity links
       → factual_validator (shadow/enforce)
       → critique OR quality_judge (flagged)
       → compose_outbound_reply
       → memory proposals (flagged)
       → TTS (flagged)
  → send_brevo_reply
  → insert_agent_response
  → remarketing sync (flagged)
```

---

## 3. Module classification

| Module | Class |
|--------|-------|
| `message_pipeline.py` | production-main |
| `openai_agent.py` | production-main (+ legacy tool-loop fallback) |
| `openai_gateway.py` | production-main |
| `openai_runtime.py` | production-main |
| `turn_runtime.py` | production-main (runtime flag) |
| `sales_agent.py` | production-main |
| `commerce_router.py` | production-main (helpers / fallback search) |
| `commerce_context.py` | production-main |
| `working_memory.py` | production-main |
| `response_composer.py` | production-main |
| `channel_profiles.py` | production-main |
| `prompt_compiler.py` | production-optional (`AGENT_DB_PERSONA_ENABLED`) |
| `factual_validator.py` | production-optional (default shadow) |
| `quality_judge.py` | shadow-default / optional |
| `response_critique.py` | production-optional (config default off; `.env.example` shadow) |
| `memory_service.py` | production-optional (default off) |
| `persona_repository.py` | production-optional (persona flag) |

None of the listed modules are fully unused.

---

## 4. OpenAI calls (estimates, critique off)

| Message type | Typical | Max core |
|--------------|---------|----------|
| Soft greeting / deterministic resume | 0 | 0 |
| Product search | 2 (interpret + responder) | ~5 (+ category/match/rerank) |
| Checkout confirmation | 2 | 2–3 (+ checkout repair) |
| Payment check (deterministic) | 0 | 2 if falls to sales |

With critique/judge shadow/enforce, +1 (or more with regenerate).

---

## 5. TRAYadaptor calls (estimates)

| Scenario | Typical | Max |
|----------|---------|-----|
| Product search | 1–6 | ~35–40 (parallel probes + pages + variants) |
| Order create + payment lookup | 3–5 | ~8 |
| Direct PIX settle | 0–1 create after approved | 1 create_order |

---

## 6. History load vs send-to-model

| Knob | Config default | `.env.example` | Role |
|------|----------------|----------------|------|
| `AGENT_HISTORY_LIMIT` | 80 | 80 | Load from DB |
| `AGENT_HISTORY_HARD_CAP` | 80 | 80 | SQL hard bound |
| `AGENT_MAX_RECENT_TURNS` | 8 | 8 | Persona compiler `input_items` only |

**Gap:** interpreter can send up to **80** turns; responder caps at **40**; persona compiler uses **8**. `AGENT_MAX_RECENT_TURNS` does not currently cap the sales interpreter.

**Load points:** `openai_agent.generate_agent_reply_async`; critique stage reload in `message_pipeline` if missing.

**Send points:** interpreter (full normalized), clarification (full), responder (`[-40:]`), prompt_compiler (last N), critique transcript (char-capped).

---

## 7. Prompt duplication

- `openai_agent.SYSTEM_INSTRUCTIONS` used as fallback and again inside `<legacy_agent_contract>` when DB persona path compiles with `extra_system_blocks`.
- When `AGENT_DB_PERSONA_ENABLED=false`, `resolve_system_instructions` returns fallback and may skip re-wrapping — sales path uses separate `SALES_*_INSTRUCTIONS`.
- Overlap themes: PT-BR, no invent price/stock/payment secrets, Tray = catalog truth, DB = raffle/balance truth.
- Commerce state / working memory can appear in multiple system blocks for interpreter/responder.

---

## 8. Flags (active / inactive / conflict risk)

| Flag | Config default | Example | Notes |
|------|----------------|---------|-------|
| `OPENAI_API_MODE` | `chat_completions` | same | Rollback-safe; Responses ready |
| `OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED` | true | true | Keeps Chat as primary |
| `OPENAI_RESPONSES_TRAFFIC_PERCENT` | 0.0 | 0.0 | Canary inactive |
| `AGENT_DB_PERSONA_ENABLED` | false | false | Persona not on main path |
| `AGENT_MEMORY_*` | false | false | Proposals off |
| `AGENT_CONVERSATION_SUMMARY_ENABLED` | false | false | Summary off |
| `AGENT_LLM_BUDGET_ENABLED` | false | false | Budget soft/off |
| `AGENT_MAX_LLM_CALLS_PER_TURN` | 2 (config) | **8** | Example conflicts with intended budget=3 |
| `AGENT_HISTORY_LIMIT` | 80 | 80 | Too large for model window goal (12) |
| `AGENT_CRITIQUE_MODE` | off (config) | **shadow** | Example enables +1 call/turn |
| `AGENT_FACTUAL_VALIDATION_MODE` | shadow | shadow | Observe only |
| `AGENT_QUALITY_JUDGE_MODE` | shadow | shadow | Skipped when critique ≠ off |
| `PIX_DIRECT_ENABLED` | false | false | PIX path gated |

---

## 9. Deterministic paths already present

- Soft greeting when no resumable commerce
- Payment / unpaid order resume
- Image product identity path
- Order confirmation confirm/reject tied to pending action
- Shipping single-option confirm
- Local raffle/balance/coupon/history replies
- Explicit human handoff
- PIX webhook settle (amount match + approved only)

---

## 10. Duplicate context consultation risks

- Commerce state in interpreter payload + working memory + responder FACTS
- Product search: parallel Tray probes for same brand/model
- Payment lookup may call `get_order_payment` twice
- History loaded once then optionally reloaded for critique
- Capability catalog injected into interpreter and critique paths

---

## 11. Approximate main prompt size (order of magnitude)

| Block | Approx chars (typical commerce turn) |
|-------|--------------------------------------|
| Sales interpreter instructions + checkout flow | 6k–12k |
| Commerce state + working memory | 1k–4k |
| Capability catalog | 1k–3k |
| History (if 40–80 turns) | 5k–40k+ |
| Responder contract + FACTS | 4k–15k |
| **Risk peak** | easily **>30k chars** / multi‑k tokens when history=80 |

---

## 12. Proposed commit division (low → high risk)

1. **baseline + suite green** — docs baseline; fix health regression; align outdated retrieval/interpreter tests to current correct contracts  
2. **prompt dedup (Fase 2)** — single authority order; no duplicate current message; prompt audit hash/chars/tokens  
3. **history window (Fase 6 slice)** — cap model window (`HISTORY_LIMIT=12`, `MAX_RECENT_TURNS=8`); keep hard_cap=80 for deterministic recovery  
4. **LLM budget (Fase 8)** — enforce max calls; zero-LLM paths; metrics for avoided calls  
5. **fact source typing + factual validator (Fases 3/10)** — structured evidence; enforce mode behind flag  
6. **selective judge (Fase 9)** — risk-triggered; critique stays off by default  
7. **persona DB on with fallback (Fase 5)** — no dual persona inject  
8. **memory proposals + contact memory inject (Fase 7)** — auto-apply off  
9. **conversation summary (Fase 6 remainder)** — async/criteria-based, not every turn  
10. **Responses canary defaults/docs (Fase 4)** — sticky routing already exists; update example/README; strengthen tool-loop fallback tests  
11. **sales_agent extract (Fase 11)** — incremental package under `app/sales/` with wrappers  
12. **response presenter / naturalness (Fase 12)**  
13. **product snapshot cache (Fase 13)** — measure first  
14. **turn metrics event (Fase 14)**  
15. **offline evals (Fase 15)**  
16. **security revalidation pass (Fase 16)** + legacy removal only after metrics

Each commit: targeted tests → related regression → document env changes.
