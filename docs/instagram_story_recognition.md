# Instagram Story ↔ product recognition (v8)

## Diagnóstico do payload

| Item | Situação |
|------|----------|
| Provedor | **Brevo Conversations** (omnichannel). Sem webhook Meta Graph direto. |
| Entrada | `POST /api/webhooks/brevo/conversations` |
| Campos usados | `reply_to` / `replyTo` / `story` / `referral` / `attachments[].type=story_mention` |
| URL operacional | `SecretStr` com query/assinatura preservada — só para download |
| URL de log | `SafeMediaReference` (host + path hash) — `strip_signed_url` só para observabilidade |
| Imagem | Suportada (streaming + magic bytes) **quando Brevo entrega URL** |
| Vídeo | Flag `INSTAGRAM_STORY_VIDEO_FRAME_ANALYSIS_ENABLED=false` até decoder validado no deploy; fallback thumbnail/print |
| Carousel | `StoryMediaItem[]` + media_type=carousel; não auto-confirma produto único |
| Diagnostics | Ver `docs/instagram_story_payload_validation.md` |
| Canary | **Bloqueado** até fixture de payload real sanitizado de produção estar coberta |

### Limitação Brevo / Instagram (produção observada)

Quando o visitante responde a um **Story** (ou envia certos anexos IG), o Brevo entrega
apenas o placeholder:

> `This message cannot be viewed in Brevo. Please go to Instagram app to view it.`

- Sem `image_url` / attachment — visão e Story recognition **não rodam**
- O placeholder chega com `role=agent` e era ignorado (e ainda tocava human takeover)
- Mitigação atual: detectar o placeholder, **não** marcar takeover humano, responder
  pedindo reenvio da foto como imagem normal no chat; em follow-up `"valor"` no
  Instagram sem mídia, mesma orientação (`app/brevo_instagram_media.py`)

> Se um ZIP legado trouxe `.env.local` com `VERCEL_OIDC_TOKEN`, **revogue o token externamente** (Vercel). O valor não deve ser commitado nem logado.

## Fluxo

```text
Webhook Brevo → InstagramStoryContext
→ resolve_story_tenant (sem fallback silencioso newstore)
→ rollout off|shadow|canary|full
→ associação DB / lease processing
→ download streaming (URL assinada completa)
→ SHA-256 → match confirmado / L1 cache / L2 DB → OpenAI visão
→ matching (EAN/SKU/ref → índice visual → Tray com score evidenciado)
→ revalidação Tray (produto + variante)
→ AgentResult + active_product / last_story_product
```

## Migrations

| Ordem | Arquivo | Pré-requisitos | Rollback | Impacto |
|------|---------|----------------|----------|---------|
| 018 | `sql/018_ai_catalog_index_variants.sql` | 017 | drop unique/catalog_item_key | variantes no índice |
| 019 | `sql/019_instagram_story_products.sql` | 018 | `DROP TABLE instagram_story_products` | associações Story |
| 020 | `sql/020_instagram_story_processing_and_catalog.sql` | 019 | drop colunas/índices 020 | lease, analysis_version, retenção, dedupe catálogo |

## Variáveis (sem segredos)

| Variável | Recomendado | Efeito | Obrigatória | Rollback |
|----------|-------------|--------|-------------|----------|
| `INSTAGRAM_STORY_RECOGNITION_ENABLED` | `false` | liga o roteamento | sim | `false` |
| `INSTAGRAM_STORY_PAYLOAD_DIAGNOSTICS` | `false` | estrutura sanitizada | não | `false` |
| `INSTAGRAM_STORY_ROLLOUT_MODE` | `off` | off/shadow/canary/full | sim | `off` |
| `INSTAGRAM_STORY_CANARY_PERCENT` | `5` | sticky canary | não | `0` |
| `INSTAGRAM_STORY_VISION_MODEL` | vazio | → MAIN → OPENAI_MODEL | não | vazio |
| `INSTAGRAM_STORY_ANALYSIS_VERSION` | `v2` | invalida cache L2 | sim | `v1` |
| `INSTAGRAM_STORY_VIDEO_FRAME_ANALYSIS_ENABLED` | `false` | decoder | não | `false` |
| `INSTAGRAM_STORY_EXACT_MATCH_MIN_CONFIDENCE` | `0.95` | auto-match exato | não | ↑ |
| `INSTAGRAM_STORY_VISUAL_MATCH_MIN_CONFIDENCE` | `0.96` | auto-match visual | não | ↑ |
| `INSTAGRAM_STORY_MATCH_MARGIN` | `0.12` | top1−top2 | não | ↑ |
| `INSTAGRAM_STORY_MEDIA_RETENTION_DAYS` | `7` | cleanup cron | não | ↑ |
| `INSTAGRAM_STORY_ACCOUNT_TENANT_MAP` | vazio | `ig:tenant` | multiempresa | vazio |
| `INSTAGRAM_STORY_STORAGE_BUCKET` | privado | storage | se storage on | vazio |

## Rollout

1. Diagnostics (recognition off)  
2. Shadow (analisa/registra, não muda resposta)  
3. Canary só após payload real + revalidação Tray  
4. Expandir 5→25→50→100  

`full` permanece desabilitado por padrão.
