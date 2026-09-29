# XNamaiAgent — atendimento comercial da Xnamai

Agente Python/FastAPI com catálogo Mercos, continuidade de produtos e carrinho local. Recebe mensagens via YCloud, Brevo e Meta Instagram, registra auditoria e usa OpenAI quando necessário.

O fluxo Mercos prepara a revisão do pedido; `create_order` continua desabilitado. Preço e estoque dependem da fonte comercial disponível.

O cadastro de cliente possui fluxo determinístico próprio: coleta PF/PJ, valida
os dados, mostra uma revisão mascarada e exige confirmação explícita antes de
`create_customer`. A mutação fica desligada por padrão e só é habilitada após
homologação com `MERCOS_CUSTOMER_MUTATIONS_ENABLED=true`. Consulte a
[comparação com a base de referência](docs/nsagent_xnamai_customer_registration_analysis.md).

Veja [operação das filas, políticas e conhecimento da persona](docs/xnamai-reliability.md).

> Este projeto não inclui credenciais reais. Configure tudo em Environment Variables na Vercel.

## Stack

- Python + FastAPI
- Vercel Python Runtime
- OpenAI Python SDK (`openai==2.7.2`) — Chat Completions (produção) + gateway Responses
- PostgreSQL/Supabase via `psycopg`
- Webhooks YCloud, Brevo e Meta Instagram
- Inbox/outbox persistentes e worker de recuperação

## OpenAI API mode (migração)

```txt
OPENAI_API_MODE=chat_completions   # default seguro / rollback
# OPENAI_API_MODE=canary           # % sticky Responses + fallback Chat (texto/structured)
# OPENAI_API_MODE=responses        # 100% Responses (+ fallback Chat se habilitado)
# OPENAI_API_MODE=shadow           # Chat em produção + sample Responses
OPENAI_RESPONSES_TRAFFIC_PERCENT=0.10
OPENAI_RESPONSES_FALLBACK_TO_CHAT=true
OPENAI_CANARY_STICKY_ROUTING=true
OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED=true
OPENAI_STORE_RESPONSES=false
OPENAI_USE_PREVIOUS_RESPONSE_ID=false
```

**Rollout sugerido**

1. `OPENAI_API_MODE=canary` + `OPENAI_RESPONSES_TRAFFIC_PERCENT=0.05` (sticky por conversa)
2. Subir para `0.10` após métricas verdes
3. `OPENAI_API_MODE=responses` + `OPENAI_CHAT_COMPLETIONS_PRIMARY_ALLOWED=false`

Tool loops **nunca** fazem fallback para Chat (evita repetir ações comerciais).
Texto/structured podem cair para Chat quando `OPENAI_RESPONSES_FALLBACK_TO_CHAT=true`.

### Persona versionada (produção)

```txt
sql/009_ai_agent_persona.sql         # ai_agent_persona_versions + ai_prompt_compilations
AGENT_DB_PERSONA_ENABLED=true        # tom/identidade do banco; fallback = contrato em código
```

Sem persona ativa/ falha de DB → usa contrato operacional em código + `<fixed_safety_policy>`.
Persona **não** pode embutir preço/estoque/link de checkout voláteis.

A persona usa `AGENT_PERSONA_TENANT_ID=xnamai` e `AGENT_PERSONA_KEY=xnamai_commercial`. O tenant comercial é independente: `COMMERCE_TENANT_ID=xnamai`.

A identidade está em `persona_xnamai.txt`. Para cadastrar em um ambiente configurado, execute `python scripts/seed_xnamai_persona.py`. O seed preserva personas existentes; revise e ative a versão correta pelo admin quando já houver uma persona cadastrada. Configurações e personas publicadas precisam ser atualizadas no ambiente de implantação para refletir esta versão.

Canais oficiais: [site institucional](https://www.xnamai.com/), [catálogo e pedidos](https://xnamai.meuspedidos.com.br/) e [XNaMai Club](https://www.clubxnamai.com.br/).

Admin (Bearer `ADMIN_API_TOKEN`):

```txt
GET/POST /api/admin/agents/{tenant_id}/personas
GET      /api/admin/agents/{tenant_id}/personas/active
POST     /api/admin/agents/{tenant_id}/personas/{id}/activate|archive|rollback
GET      /api/admin/agents/{tenant_id}/prompt-preview
GET/POST /api/admin/agents/{tenant_id}/instruction-extensions
POST     /api/admin/agents/{tenant_id}/instruction-extensions/{id}/approve|reject
GET/POST /api/admin/agents/{tenant_id}/contacts/{sender_key}/memories
DELETE   /api/admin/agents/{tenant_id}/contacts/{sender_key}/memories/{memory_key}
```

### Memória (propostas + inject)

```txt
sql/010_ai_memory_proposals.sql
AGENT_MEMORY_PROPOSALS_ENABLED=true           # envelope estruturado + persistência
AGENT_CONTACT_MEMORY_IN_PROMPT_ENABLED=true   # injeta memórias ativas no prompt
AGENT_MEMORY_AUTO_APPLY_ENABLED=false         # manter off até allowlist
AGENT_CONVERSATION_SUMMARY_ENABLED=false      # critérios/async; não a cada turno
AGENT_INSTRUCTION_EXTENSION_PROPOSALS_ENABLED=false
AGENT_LEARNING_AUTO_PROMOTE=false             # Etapa 9: insights pending only
AGENT_LEARNING_AUTO_ACTIVATE=false            # nunca ativar extension sem admin
```

Com propostas ligadas, o responder comercial usa `AgentTurnEnvelope` (reply + proposals)
na mesma chamada; o backend valida (`memory_policy`) e grava em `ai_memory_proposals`.
Summary só aplica delta com progresso real (compromisso, correção, falha, meta nova…).

Auto-apply exige **ambos**:
`AGENT_MEMORY_AUTO_APPLY_ENABLED=true` **e** `AGENT_MEMORY_AUTO_APPLY_SENDER_ALLOWLIST`
(lista de `sender_key` ou `*`). Thresholds: confidence ≥ 0.85, importance ≥ 0.70,
kinds allowlisted, evidência explícita. Extensões tenant e attendance learning
nunca auto-ativam (approve só via admin).

## Arquivos principais

```txt
api/index.py                         # FastAPI app para Vercel
app/webhook_parser.py                # Parser defensivo do payload Brevo
app/openai_agent.py                  # Chamada OpenAI + instruções do agente
app/openai_gateway.py                # Gateway Chat Completions / Responses / shadow
app/response_presenter.py            # Naturalidade / regras de apresentação
app/commerce/mercos/                  # provider, normalização e sincronização Mercos
app/ingress/                         # filas persistentes e workers
app/business_policy.py               # políticas publicadas com a persona
app/persona_knowledge.py             # recuperação documental com fontes e validade
scripts/process_queues.py            # consumidor periódico de inbox/outbox
app/turn_metrics.py                  # Evento turn.quality (sem PII)
app/sales/                           # Extração incremental do sales_agent
app/prompt_compiler.py               # Compila instructions (persona + overlays)
app/persona_repository.py            # CRUD versionado da persona
tests/evals/                         # Offline eval honestos (`pytest -m offline_eval`)
.env.example                         # Variáveis sem segredos reais
vercel.json                          # Config Vercel
```

Replay offline (agente real + fakes; score não copia `expected`):

```bash
pytest -m offline_eval
```

Detalhes: `docs/agent_generative_migration_etapa11.md`.

Canary progressivo / rollback (Etapa 12): `AGENT_ROLLOUT_PROFILE=canary_5|…|full` ou `AGENT_EMERGENCY_ROLLBACK=true`. Status: `GET /api/health` → `rollout` ou `GET /api/admin/rollout`. Doc: `docs/agent_generative_migration_etapa12.md`.

## Segurança obrigatória

Configure as chaves no ambiente do serviço e do worker:

```txt
OPENAI_API_KEY
DATABASE_URL
BREVO_API_KEY
BREVO_WEBHOOK_SECRET
ADMIN_API_TOKEN
YCLOUD_API_KEY
YCLOUD_WEBHOOK_SECRET
CRON_SECRET
```

Não suba `.env` para GitHub.

## Deploy na Vercel

1. Suba este projeto para um repositório.
2. Na Vercel, importe o repositório.
3. Configure as Environment Variables usando `.env.example` como referência.
4. Aplique as migrações necessárias de `sql/` em ordem. As filas precisam de `022_inbound_inbox_outbox.sql`; consulte o [guia operacional](docs/xnamai-reliability.md) antes de ativá-las.
5. Configure na Brevo o webhook apontando para:

```txt
https://SEU-DOMINIO.vercel.app/api/webhooks/brevo/whatsapp
```

6. Configure na Brevo um header customizado:

```txt
X-Webhook-Token: mesmo_valor_de_BREVO_WEBHOOK_SECRET
```

## Teste local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./scripts/local_dev.sh
```

Health check:

```bash
curl http://localhost:8000/api/health
```

Teste do agente:

```bash
curl -X POST http://localhost:8000/api/test/agent \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Olá, preciso de atendimento", "phone":"554399999999", "name":"Teste"}'
```

## Dry-run

Por padrão:

```txt
DRY_RUN=true
BREVO_REPLY_MODE=dry_run
```

Assim o webhook recebe, registra, chama o agente e simula o envio sem mandar mensagem real.

Só desative depois de validar o endpoint outbound correto da Brevo para sua conta:

```txt
DRY_RUN=false
BREVO_REPLY_MODE=brevo
BREVO_SEND_URL=https://...
```

## Limites intencionais

As capacidades comerciais são expostas pelo provider. Esta versão não cria pedidos no Mercos e o transporte YCloud continua limitado a texto. Configuração por workspace no painel, ingestão de anexos e suporte a mídia exigem integração própria; publicar documentos em `metadata.knowledge_documents` já permite utilizá-los nos prompts.
