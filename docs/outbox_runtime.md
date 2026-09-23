# Outbox — runtime de entrega

## Decisão de execução

| Opção | Veredito | Motivo |
| --- | --- | --- |
| A. Worker persistente (`scripts/process_queues.py`) | **Não** no deploy atual | A aplicação roda na Vercel (serverless): não há processo contínuo. Continua válido se o agente for hospedado fora da Vercel — nesse caso **substitui** a opção B, não soma. |
| B. Cron chamando `/api/cron/process-outbox` a cada 1–5 min | **Escolhida**, via GitHub Actions (`.github/workflows/outbox-retry.yml`, a cada 5 min) | Cron sub-diário da Vercel depende do plano (no Hobby o deploy falha); o projeto já usa GitHub Actions + `CRON_SECRET` para chamar endpoints de cron (`attendance-learning.yml`). |
| C. Equivalente existente | Mantido só como backstop | `vercel.json` roda `/api/cron/process-inbox` 1x/dia (inbox + outbox). |

Consumidor **único**: `app.ingress.outbox_worker.process_outbox_batch`. Os três
gatilhos (envio inline, workflow de 5 min, cron diário) chamam a mesma função;
`FOR UPDATE SKIP LOCKED` + lease tornam sobreposição inofensiva. Não ative o
worker persistente junto com o workflow.

Segredos do GitHub necessários: `AGENT_BASE_URL`, `CRON_SECRET` (os mesmos do
`attendance-learning.yml`). Nada foi implantado por esta alteração.

## Fluxo

```
Producer   worker de inbox gera a resposta e grava o envelope completo
           (enqueue_accepted_outbound) ANTES de chamar o canal
Storage    public.ai_outbound_outbox (sql/022), 1 linha por inbound
Consumer   1ª tentativa inline (dispatch_accepted_outbound, lease "inline:*")
           retentativas: process_outbox_batch (workflow 5 min / cron diário)
```

## Estados

```
pending --claim--> leased --send ok--> sent
                     |
                     +--falha definitiva--> failed --(backoff)--> leased ...
                     +--4xx / esgotou / janela / superseded--> dead
                     +--ambígua--> dead (last_error = delivery_unknown:*)
dead(delivery_unknown) --callback YCloud entregue--> sent
dead(delivery_unknown) --callback YCloud falhou----> dead (delivery_failed_confirmed:*)
```

## Intervalo e retry

- Atraso antes da tentativa N+1: `min(AGENT_QUEUE_RETRY_MAX_SECONDS, AGENT_QUEUE_RETRY_BASE_SECONDS * 2^(N-1))` — 30s, 60s, 120s, 240s, 300s…
- A retentativa só acontece quando o consumidor roda; com o workflow de 5 min o
  intervalo real é o maior entre o backoff e o próximo ciclo.
- Janela: `AGENT_OUTBOX_RETRY_WINDOW_SECONDS`. Default do código 900s (≈3
  tentativas com ciclos de 5 min). **Recomendado 1800s** com este agendador
  (≈6 tentativas); o `.env.example` já traz esse valor. Resposta obsoleta não é
  enviada mesmo dentro da janela: se o cliente mandou outra mensagem, a linha
  vira `dead` (`superseded_by_later_inbound`).
- Teto de tentativas: `max_attempts` (8, schema) — na prática limitado pela janela.

## Lease e recuperação de crash

- Claim atômico: `UPDATE … FROM (SELECT … FOR UPDATE SKIP LOCKED)`; dois
  consumidores nunca pegam a mesma linha.
- Lease: `AGENT_OUTBOX_LEASE_SECONDS` (180s). Se o processo morre com a linha
  `leased`, ela volta a ser elegível quando `lease_expires_at < now()`.
- Recibo (`sent`/`failed`) exige o dono atual com lease vivo: um worker que
  perdeu o lease não sobrescreve o resultado de outro.
- Antes de reenviar, o worker confere: lease, auditoria de envio já existente
  (recupera o recibo sem reenviar), mensagem posterior do cliente e takeover humano.

## Dead-letter

`status = 'dead'` com `last_error`:

| `last_error` | Significado | Ação |
| --- | --- | --- |
| `attempts_exhausted` / `outbox_retry_window_expired` | falhas definitivas até o limite | investigar provider |
| `superseded_by_later_inbound` | cliente seguiu a conversa | nenhuma |
| `human_takeover` | atendimento humano assumiu | nenhuma |
| erro 4xx (ex.: `ycloud_bad_request`) | rejeição permanente | corrigir dado/config |
| `delivery_unknown:*` | pode ter sido entregue — sem retry automático | aguardar reconciliação |
| `delivery_failed_confirmed:<code>` | provider confirmou falha | investigar código |

Não volte linhas `dead` para `pending` em massa.

## Reconciliação de `delivery_unknown`

`app/ingress/delivery_reconciliation.py`, acionada pelo webhook
`whatsapp.message.updated` do YCloud (`/api/webhooks/ycloud/whatsapp`):

1. HMAC do webhook (já existente) **e** idade do timestamp assinado ≤
   `YCLOUD_STATUS_WEBHOOK_MAX_AGE_SECONDS` (default 24h; futuro > 5 min rejeitado);
2. `externalId` precisa casar `^outbox:<id numérico>$`;
3. a linha precisa existir, ser do provider `ycloud` e o destinatário do
   callback precisa ser o da linha;
4. `accepted`/`sent`/`delivered`/`read` → `sent`; `failed` → falha confirmada;
   qualquer outro status → nada muda;
5. transições em UPDATE único com guarda de estado + `event_id` registrado:
   callback repetido é no-op e estado terminal não regride.

Reconciliar **nunca** envia mensagem. Meta e Brevo não devolvem correlação para
um envio cuja resposta se perdeu: seus `delivery_unknown` só se resolvem manualmente.
