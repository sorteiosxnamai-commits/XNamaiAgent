# NSAgentForSorteios v6 — technical correction report

Incremental architectural fixes (packaging → fact authority → catalog index →
LLM budget → Responses controls → evals → rollout). Models
(`OPENAI_MAIN_MODEL` / `OPENAI_FAST_MODEL` / `OPENAI_MODEL`) were **not** changed.

## Baseline → current

| Metric | Before | After |
|--------|--------|-------|
| `python -m pytest -q` | 955 passed | **982 passed, 1 skipped** |
| Offline eval | present | 30 passed (`-m offline_eval`) |
| Bare `pytest` on this Windows host | not on PATH | use `python -m pytest` (pythonpath=`.` configured) |

## What was connected to the real flow

1. **Fact authority** — `authorize_products_for_responder` / `GroundedCommerceEvidence` in validator + composer; persona/memory cannot invent price/stock.
2. **Catalog variants** — migration `sql/018_ai_catalog_index_variants.sql` + upsert on `(tenant_id, catalog_item_key)`.
3. **Index read** — `CatalogIndexRepository` seeds discovery in `sales_agent` recommendation path; Tray remains final authority; fallback logged as `catalog_index_fallback`.
4. **CandidateTrace** — built during hybrid rank; attached under `_retrieval` (obs only).
5. **Allowed ID sets** — closed sets on rerank + composer; stamped on product search results.
6. **Revalidation** — top-N Tray refresh; total/partial failure paths; only confirmed products presented.
7. **LLM budget** — middleware uses `build_llm_call_budget`; logical vs transport metrics; promote without reset; Responses→Chat refunds logical only.
8. **Responses controls** — skip reasons when capability not declared; no silent attach of unsupported params.
9. **Summary mode** — `AGENT_CONVERSATION_SUMMARY_MODE=off|shadow|enforce`; shadow generates/logs, never injects, does not apply summary delta.
10. **Learning** — cron cannot approve/activate with `attendance_learning_cron`.
11. **Rollout** — profile `shadow` + canaries + emergency; sticky `tenant_id`+hashed conversation; admin GET `/api/admin/rollout`.
12. **Packaging** — `scripts/package_release.py` + secret pattern scan (values never printed).

## Migrations

| Order | File | Risk | Notes |
|-------|------|------|-------|
| 017 | existing catalog index | — | keep; do not rewrite if already applied |
| **018** | `sql/018_ai_catalog_index_variants.sql` | medium | backfill `catalog_item_key`, dedupe, unique `(tenant_id, catalog_item_key)` |

Apply on the app DB (same process as prior SQL files). Rollback: documented in the SQL file comments — restore unique on `(tenant_id, product_id)` only after confirming no multi-variant rows are needed.

## Env vars (non-secret)

| Variable | Recommended | Required | Effect | Rollback |
|----------|-------------|----------|--------|----------|
| `AGENT_CATALOG_INDEX_READ_ENABLED` | `true` | no | Read index for discovery | `false` |
| `AGENT_CATALOG_INDEX_WRITE_ENABLED` | `true` | no | Upsert after search | `false` |
| `AGENT_CATALOG_INDEX_FALLBACK_TO_TRAY` | `true` | no | Tray when index empty | keep `true` |
| `AGENT_CATALOG_INDEX_MAX_AGE_SECONDS` | `86400` | no | Stale discovery TTL | raise / disable read |
| `AGENT_CATALOG_INDEX_CANDIDATE_LIMIT` | `30` | no | Index candidate cap | lower |
| `AGENT_LLM_BUDGET_ENABLED` | `true` | no | Enforce per-turn budget | `false` |
| `AGENT_MAX_LLM_CALLS_PER_TURN` | `2` | no | Normal logical cap | raise |
| `AGENT_MAX_LLM_CALLS_PER_TURN_COMPLEX` | `4` | no | Complex cap | raise |
| `AGENT_CONVERSATION_SUMMARY_MODE` | `off` then `shadow` | no | Summary lifecycle | `off` |
| `AGENT_ROLLOUT_PROFILE` | start `shadow`/`canary_5` | no | Progressive rollout | `emergency` |
| `AGENT_EMERGENCY_ROLLBACK` | `false` | no | Kill switch | `true` |
| `AGENT_REVALIDATE_TOP_N` | `3` | no | Live Tray refresh count | keep |
| `RUN_ONLINE_OPENAI_EVALS` | unset | no | Enables `@online_eval` | leave unset |
| `OPENAI_*` models | **unchanged** | yes | User-chosen models | do not change |

Preserve: Responses primary, Chat fallback, `OPENAI_STORE_RESPONSES=false`, `OPENAI_USE_PREVIOUS_RESPONSE_ID=false`, factual enforce, critique shadow, judge off, presenter thin, learning auto off.

## External pendências

1. **Rotate** any `VERCEL_OIDC_TOKEN` that may have been packaged historically (revoke in Vercel; do not paste values here).
2. **Apply migration 018** on production / staging DB.
3. Confirm Render/Vercel env matches `.env.example` for new flags.
4. Run online evals only with `RUN_ONLINE_OPENAI_EVALS=true` + key (no side effects).
5. Initial rollout: `shadow` → `canary_5` → … → `full`; monitor `/api/admin/rollout` alerts.
6. Ensure `ai_human_takeover_state` table exists in prod (prior ops note).

## Comparativo (qualitativo)

| Area | Before | After |
|------|--------|-------|
| Calls/turn | Budget helper unused / weak | Middleware enforces; logical≠transport |
| Retrieval | Tray-only then write index | Index seed + Tray + revalidate |
| Factual | Validator present | Authority filters composer evidence |
| Security packaging | ZIP could include secrets | Scanner + gitignore + docs |
| Observability | Call count only | + transport / responses / chat fallback |
| Evals | Deterministic empty-key replay | + structured fake gateway; online gated |
