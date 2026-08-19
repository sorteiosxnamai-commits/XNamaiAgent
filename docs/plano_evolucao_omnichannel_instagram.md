# Plano completo de evolução — NSAgent omnichannel + Instagram sem limitações

Fonte: auditoria `deep-research-report.md` + estado atual do repositório (`main` @ pós-`9765177`).

**Objetivo de produto:** Instagram DM funcional de ponta a ponta — Stories, imagens encaminhadas, fotos normais — com identificação de produto grounded em catálogo/Tray, sem depender da mídia da Brevo.

**Princípio:** não reescrever tudo. Extrair módulos, colocar infraestrutura de eventos, e substituir o ponto frágil (ingress de mídia Instagram) por **Meta Graph direto**.

**Decisões fechadas (sem perguntas):**

| Tema | Decisão |
|------|---------|
| Ingress Instagram | Meta Messaging Webhooks + Graph API (fonte de mídia). Brevo permanece opcional como inbox humano / WhatsApp. |
| Outbound Instagram | Meta Send API (Graph) para respostas do agente no IG; Brevo Conversations só se `visitorId` existir e canal for Brevo. |
| WhatsApp | Continua Brevo no curto prazo; adapter Meta WhatsApp fica preparado mas não bloqueia Instagram. |
| Fila | Postgres-backed inbox + outbox + worker HTTP cron / Vercel queue-style poller (sem Redis obrigatório na V1). |
| Índice visual | Nova tabela `ai_product_image_index_v2` tenant-scoped; v1 permanece read-only até cutover. |
| RAG | Conectar `EvidencePackage` ao `prompt_compiler`; knowledge base versionada em Postgres (não preços em `site_knowledge`). |
| Rollout Instagram | shadow → canary → full com fixtures reais Meta + flag `INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED`. |
| Tenant | Sem fallback silencioso; `tenant_id` obrigatório em matching visual e Story. |

---

## Mapa do estado atual (baseline)

| Área | Hoje | Bloqueio |
|------|------|----------|
| Canais Brevo | `BREVO_ALLOWED_CHANNELS=whatsapp,facebook` (IG off) | IG não atende |
| Story recognition | Código existe; flags `false` / rollout `off` | Não opera |
| Mídia Story via Brevo | Placeholder “cannot be viewed” sem URL | Visão impossível |
| Image search | Funciona se Brevo entregar `image_url` | IG Story não entrega |
| Meta Graph | **Inexistente** | Sem URL CDN oficial |
| Webhook | 100% síncrono | Timeout / retries |
| CI | Só cron attendance-learning | Regressão livre |
| Índice visual | `ai_product_image_index` sem `tenant_id` | Multi-loja inseguro |
| RAG | `relevant_knowledge` descartado | Sem grounding institucional |

---

## Arquitetura-alvo (resumo)

```text
Meta IG Webhook (assinado) ──┐
Brevo Conversations ─────────┼──► Channel Adapter → NormalizedInbound
                              │         │
                              │         ▼
                              │   ai_inbound_inbox (durável, idempotent)
                              │         │
                              │    HTTP 200 imediato
                              │         ▼
                              │   Worker (poll / cron / queue)
                              │         │
                    ┌─────────┼─────────┼─────────┐
                    ▼         ▼         ▼         ▼
              Multimodal  Orchestrator  Tools   Identity
              (Story/img)  (sales plan)  Tray   tenant
                    │         │
                    ▼         ▼
              EvidencePack → FactualGuard → Outbox → Channel Sender
                                                    (Meta Send / Brevo)
```

---

## Fases de implementação

Cada fase tem: entregáveis, arquivos, critérios de aceite, ordem de PRs. Executar em série P0→P1→P2→P3; PRs pequenos e mergeáveis.

---

### FASE 0 — Contenção de segurança (1–2 dias)

**Por quê:** token/OIDC em artifact é risco imediato.

| # | Entrega | Detalhe |
|---|---------|---------|
| 0.1 | Rotação | Revogar `VERCEL_OIDC_TOKEN` (e qualquer segredo do ZIP legado) no dashboard Vercel |
| 0.2 | CI secrets | `.github/workflows/ci.yml` + `scripts/scan_secrets.py` no pipeline (fail closed) |
| 0.3 | Release | Reforçar `package_release.py` allowlist; job `release.yml` dry-run |
| 0.4 | Docs | Checklist de incidente em `docs/security_incident_oidc.md` |

**Aceite:** CI falha se `.env*` / JWT / `VERCEL_OIDC` entrarem no tree/artifact; incidente documentado como “rotated”.

**PR:** `chore/p0-security-ci`

---

### FASE 1 — CI + quality gates (2–3 dias)

| # | Entrega | Detalhe |
|---|---------|---------|
| 1.1 | `ci.yml` | ruff, pytest (unit), `package_release --dry-run`, scan_secrets |
| 1.2 | Job DB | Postgres service + migrações 001–021 + subset integration |
| 1.3 | Agent evals | Job opcional/manual `agent-evals.yml` (offline) |
| 1.4 | Coverage floor | Começar em 0 reportado; gate mínimo só em módulos novos |

**Aceite:** PR sem CI verde não mergeia em `main`.

**PR:** `chore/p0-ci-quality`

---

### FASE 2 — Inbox + outbox + worker (webhook resiliente) (5–7 dias)

**Problema:** `api/index.py` processa o agente inteiro no request Brevo/Meta.

| # | Entrega | Detalhe |
|---|---------|---------|
| 2.1 | SQL | `sql/022_inbound_inbox_outbox.sql`: `ai_inbound_inbox`, `ai_outbound_outbox`, status, attempts, lease |
| 2.2 | Ingress service | `app/ingress/inbox.py`: persist payload sanitizado + idempotency key `(provider, message_id)` |
| 2.3 | Webhook thin | Brevo/Meta: validate → enqueue → **200** |
| 2.4 | Worker | `POST /api/cron/process-inbox` (e/ou loop Vercel): claim lease → `process_incoming_message` → outbox |
| 2.5 | Outbox sender | Worker envia via channel adapter; retry com backoff; dedupe send |
| 2.6 | Observabilidade | eventos `inbox.enqueued`, `inbox.processed`, `outbox.sent`, `outbox.failed` |

**Aceite:** turno com visão >15s não causa timeout do provedor; redelivery não duplica resposta (`already_sent` / outbox unique).

**PR:** `feat/p0-inbox-outbox`

**Nota:** manter caminho sync atrás de flag `AGENT_ASYNC_INGRESS_ENABLED` (default true em preview, canary em prod).

---

### FASE 3 — Contrato de canais + Meta Instagram (núcleo “IG sem limitações”) (10–14 dias)

Esta é a fase que **desbloqueia Stories e mídias**.

#### 3.1 Adapter Meta

| Arquivo | Responsabilidade |
|---------|------------------|
| `app/channels/base.py` | Protocol: parse, verify, send, fetch_media |
| `app/channels/meta_instagram.py` | Webhook Messaging IG + Send API |
| `app/channels/brevo.py` | Extrair lógica atual do parser/client |
| `app/channels/capabilities.py` | `supports_story_media`, `supports_audio`, etc. |

#### 3.2 Endpoints

| Método | Path | Função |
|--------|------|--------|
| GET | `/api/webhooks/meta` | Hub challenge (`hub.verify_token`) |
| POST | `/api/webhooks/meta` | `X-Hub-Signature-256` sobre body cru → inbox |
| POST | (existente) Brevo | Thin enqueue |

#### 3.3 Config (novas env)

```text
META_APP_SECRET=
META_VERIFY_TOKEN=
META_PAGE_ACCESS_TOKEN=          # ou IG user token de longa duração
META_IG_BUSINESS_ACCOUNT_ID=
META_WEBHOOK_ENABLED=true
INSTAGRAM_INGRESS_PROVIDER=meta  # meta | brevo | dual
BREVO_ALLOWED_CHANNELS=whatsapp,facebook   # IG volta via Meta, não Brevo
```

#### 3.4 Normalização Meta → `IncomingMessage`

Mapear:

- `message.text`
- `message.attachments[]` (image/video/audio/file) → `image_url` / `audio_url` via Graph `media_url` se necessário
- `message.reply_to.story` / `story_mention` → `InstagramStoryContext`
- `message.shares` / forwarded attachments → mesmo pipeline de imagem
- `sender.id` → `sender_key=instagram:{igsid}`
- `recipient.id` → account → `tenant_id` via `INSTAGRAM_STORY_ACCOUNT_TENANT_MAP`

#### 3.5 Obtenção de mídia (ordem)

1. URL no attachment (se assinada e HTTPS)
2. Senão Graph: `GET /{media_id}?fields=media_url,mime_type`
3. Download SSRF-safe existente (`instagram_story_media.py` / extrair para `app/multimodal/downloader.py`)
4. Hash + storage privado

#### 3.6 Outbound

- `send_meta_instagram_reply(igsid, text, optional_image)`
- Não usar Brevo `visitorId` para conversas originadas no Meta

#### 3.7 Reativar canal Instagram (somente Meta)

- `BREVO_ALLOWED_CHANNELS` permanece sem `instagram` **ou** dual-mode: Brevo IG só texto/guia, mídia só Meta
- Preferência: **`INSTAGRAM_INGRESS_PROVIDER=meta`** exclusivo para IG

**Aceite:**

- [ ] Story reply com imagem/vídeo chega com bytes baixáveis
- [ ] Imagem encaminhada (forward) vira `image_url` e entra em image search
- [ ] Foto DM normal identifica produto (quando match)
- [ ] Fixture sanitizada real em `tests/fixtures/meta_instagram/`
- [ ] Assinatura inválida → 401; challenge OK

**PRs:**

1. `feat/meta-webhook-verify-parse`
2. `feat/meta-media-fetch-ssrf`
3. `feat/meta-send-api`
4. `feat/instagram-reenable-via-meta`

---

### FASE 4 — Multimodal unificado (Stories + imagens + vídeo) (7–10 dias)

Reutilizar módulos Story existentes; unificar entrada.

| # | Entrega | Detalhe |
|---|---------|---------|
| 4.1 | `MediaUnderstanding` | Schema estruturado (relatório §Stories) em `app/multimodal/schemas.py` |
| 4.2 | Router multimodal | Uma entrada: Story reply / mention / DM image / forward / reel |
| 4.3 | Matching | Ordem: EAN/QR/ref → story mapping DB → product tags → visual embedding → OCR fusion |
| 4.4 | Confiança | `top1`, margem `top1-top2`, `live_product_verified` obrigatório para preço |
| 4.5 | Vídeo | Amostrar frames (flag on após decoder validado no runtime Vercel); consolidar evidências |
| 4.6 | Coalesce | Meta às vezes separa attachment e texto; estender `inbound_coalesce` para follow-up `valor` **com** `image_url` recente (quando houver) |
| 4.7 | Flags | Ligar recognition em shadow → canary → full após `REAL_PAYLOAD_VALIDATED=true` |

**Fluxo de identificação (obrigatório):**

```text
mídia disponível?
  não → pedir reenvio (só se Meta também falhou)
  sim → hash → L1/L2 → vision structured → matcher → Tray revalidate
       → preço/estoque só com ProductEvidence
```

**Aceite:** evals offline com ≥N fixtures Meta; zero preço inventado; ambíguo pergunta clarificação.

**PR:** `feat/multimodal-unified-matcher`

---

### FASE 5 — Índice visual v2 tenant-safe (5–7 dias)

| # | Entrega | Detalhe |
|---|---------|---------|
| 5.1 | SQL | `sql/023_ai_product_image_index_v2.sql` (schema do relatório) |
| 5.2 | Repo | Toda query exige `tenant_id` |
| 5.3 | Backfill | Job cron: Tray/catalog → captions + embeddings por tenant |
| 5.4 | Cutover | Flag `AGENT_VISUAL_INDEX_V2=true`; desligar writes em v1 |
| 5.5 | Score fusion | pesos iniciais do relatório; calibrar com evals |

**Aceite:** impossível consultar sem tenant; teste de isolamento A/B passa.

**PR:** `feat/visual-index-v2-tenant`

---

### FASE 6 — Decomposição do orquestrador comercial (7–10 dias)

Extrair `_handle_sales_message_inner` / `generate_agent_reply_async` sem mudar comportamento.

| Passo | Módulo | Conteúdo |
|-------|--------|----------|
| 6.1 | `app/conversation/context.py` | `TurnContext` |
| 6.2 | `app/understanding/` | wrappers sobre `turn_understanding` |
| 6.3 | `app/policy/engine.py` | bloqueios, scope, takeover |
| 6.4 | `app/retrieval/` | catalog + knowledge planner |
| 6.5 | `app/generation/` | responder + factual_guard existentes |
| 6.6 | Orquestrador fino | sequência do relatório (`handle_sales_turn`) |

**Aceite:** suite atual ≥ verde; arquivos novos cobertos; `sales_agent.py` reduz responsabilidades (não precisa zerar linhas de uma vez).

**PRs:** um por extração (`refactor/extract-turn-context`, etc.).

---

### FASE 7 — RAG institucional conectado (5–7 dias)

| # | Entrega | Detalhe |
|---|---------|---------|
| 7.1 | SQL | `ai_knowledge_documents` (tenant, slug, body, embedding, valid_from/to, version) |
| 7.2 | Ingest | Admin API ou script YAML → embed |
| 7.3 | Planner | Só busca quando domínio institucional / FAQ |
| 7.4 | Compiler | Remover `del relevant_knowledge`; injetar `EvidencePackage` |
| 7.5 | Limpeza | Remover preços exemplificativos de `site_knowledge.py` |

**Aceite:** resposta institucional cita evidência; preço continua só via Tray/tools.

**PR:** `feat/rag-evidence-package`

---

### FASE 8 — Observabilidade + LGPD (4–6 dias)

| # | Entrega |
|---|---------|
| 8.1 | `trace_id` ponta a ponta (já parcial) + spans: ingress, media, vision, match, tray, gen, send |
| 8.2 | Dashboard/admin: health Meta + taxas de match Story |
| 8.3 | Retenção: conversas, mídia, embeddings (além de Story 7d) |
| 8.4 | Endpoint exclusão por `sender_key` / consentimento |

**PR:** `feat/obs-lgpd-retention`

---

### FASE 9 — Ampliação (após IG estável) (contínuo)

- Meta WhatsApp Cloud API (opcional)
- TikTok adapter (stub)
- Voz realtime
- Model registry + promoção por evals
- Fine-tuning só com dataset grounded

---

## Ordem de execução no Cursor (checklist diária)

```text
Semana 1
  [ ] FASE 0 segurança + FASE 1 CI
  [ ] FASE 2 inbox/outbox (flag canary)

Semana 2–3
  [ ] FASE 3 Meta webhook + media + send
  [ ] Fixture real sanitizada + REAL_PAYLOAD_VALIDATED
  [ ] Reativar Instagram via Meta (Brevo IG continua off)

Semana 3–4
  [ ] FASE 4 multimodal unificado + Story rollout shadow→canary
  [ ] FASE 5 visual index v2

Semana 5–6
  [ ] FASE 6 refactor orquestrador
  [ ] FASE 7 RAG
  [ ] FASE 8 obs/LGPD
  [ ] Canary→full Instagram

Semana 7+
  [ ] FASE 9 expansão canais
```

---

## Instagram — critérios “sem limitações” (definição de pronto)

O Instagram só é considerado **pronto** quando **todos** forem verdadeiros:

1. Webhook Meta verificado e assinado em produção.
2. Story reply / story mention / DM image / imagem encaminhada produzem mídia baixável (não placeholder Brevo).
3. Visão estruturada + matcher + Tray revalidate antes de preço.
4. Ambíguo → pergunta; nunca inventa SKU/preço.
5. Tenant isolado no índice visual e nas associações Story.
6. Ingress assíncrono (inbox) com outbox idempotente.
7. Rollout `full` com evals e `REAL_PAYLOAD_VALIDATED=true`.
8. CI verde no PR.
9. Brevo “cannot be viewed” não é mais o caminho crítico (apenas fallback/legado).

---

## Variáveis de ambiente (estado final desejado)

```text
# Canais
BREVO_ALLOWED_CHANNELS=whatsapp,facebook
BREVO_SOCIAL_CHANNELS_ENABLED=true
INSTAGRAM_INGRESS_PROVIDER=meta
META_WEBHOOK_ENABLED=true
META_APP_SECRET=...
META_VERIFY_TOKEN=...
META_PAGE_ACCESS_TOKEN=...
META_IG_BUSINESS_ACCOUNT_ID=...

# Story / multimodal
INSTAGRAM_STORY_RECOGNITION_ENABLED=true
INSTAGRAM_STORY_ROLLOUT_MODE=full          # só após canary
INSTAGRAM_STORY_REAL_PAYLOAD_VALIDATED=true
INSTAGRAM_STORY_VIDEO_FRAME_ANALYSIS_ENABLED=true  # após validar runtime
INSTAGRAM_STORY_ACCOUNT_TENANT_MAP=ig_business_id:newstore

# Infra
AGENT_ASYNC_INGRESS_ENABLED=true
AGENT_VISUAL_INDEX_V2=true
AGENT_IMAGE_SEARCH_ENABLED=true
```

---

## Riscos e mitigações

| Risco | Mitigação |
|-------|-----------|
| Meta não libera Story CDN em alguns tipos | Fallback: pedir foto DM; log `media_fetch_failed` |
| Token Graph expira | Refresh job + alerta; rotação documentada |
| Latência visão | Inbox async + lease; resposta “analisando…” opcional só se produto exigir |
| Regressão WhatsApp | Feature flags; testes Brevo intactos |
| Custo embeddings/visão | Cache L2 por hash; thresholds altos; canary % |

---

## Fora de escopo nesta jornada (explícito)

- Reescrita total em microserviços
- Trocar OpenAI por outro LLM provider (registry sim; multi-vendor não)
- Fine-tuning antes de evals grounded
- TikTok produção antes de IG `full`

---

## Como executar no Cursor daqui pra frente

1. Abrir este doc como fonte de verdade.
2. Implementar **uma fase por PR**, na ordem FASE 0 → 9.
3. Não pausar para perguntas de produto: decisões acima prevalecem.
4. Se Meta App / tokens ainda não existirem no projeto Vercel, criar App Meta Business + Instagram Messaging product e preencher env **antes** do merge da FASE 3 em produção.
5. Após FASE 3+4 em canary, só então `BREVO_ALLOWED_CHANNELS` pode incluir IG se dual-mode for desejado; default permanece IG=Meta-only.

### Progresso

| Fase | Status |
|------|--------|
| Doc deste plano | feito |
| FASE 0 — scan_secrets + CI workflow + doc OIDC | feito (`e009793`+) |
| FASE 1 — CI pytest no GitHub Actions | feito (`.github/workflows/ci.yml`) |
| FASE 2 — inbox/outbox SQL + cron + flag async | feito (worker processa agente de verdade) |
| FASE 3 — Meta webhook verify/parse/send + worker | feito (inline process após enqueue; send graph.instagram.com) |
| FASE 4 — multimodal / Story live media bypass | parcial (`meta_live_media` + attach recent image) |
| FASE 5+ | pendente |

---

## Relação com o relatório de auditoria

| Prioridade relatório | Fase deste plano |
|----------------------|------------------|
| P0 segurança / tenant | 0, 5 |
| P0 CI | 1 |
| P0 inbox/fila/outbox | 2 |
| P1 contrato canais / Meta | 3 |
| P1 decomposição | 6 |
| P1 observabilidade | 8 |
| P2 RAG / catálogo / visual | 5, 7 |
| P2 Meta Stories confiável | 3, 4 |
| P3 vídeo/voz/TikTok | 4 (vídeo), 9 |
| P4 evals/model routing | 9 |
