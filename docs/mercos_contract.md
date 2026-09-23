# Contrato comercial — MercosAdaptor

Fonte: `sorteiosxnamai-commits/MercosAdaptor` @ `38f0bf4` (16 arquivos, lidos por
inteiro, incluindo os testes).

O XNamai **nunca** fala com a Mercos. Toda chamada vai para o MercosAdaptor,
autenticada por chave interna (`X-API-Key`). Os tokens da Mercos vivem somente no
adaptador.

---

## 1. Rotas disponíveis

| Rota | Uso | Filtro aceito |
| --- | --- | --- |
| `GET /health` | saúde do adaptador | — |
| `GET /v1/resources` | recursos suportados | — |
| `GET /v1/{resource}` | listagem incremental | **apenas** `alterado_apos` |
| `GET /v1/{customers\|products\|orders}/{mercos_id}` | detalhe | — |
| `POST/PUT /v1/customers` | criar/alterar cliente | — |
| `POST/PUT /v1/orders` | criar/alterar pedido (Mercos v2) | — |
| `POST/PUT /v1/titles` | criar/alterar título | — |

Recursos de listagem: `customers`, `products`, `orders`, `price-tables`,
`payment-conditions`, `carriers`, `commercial-policies`, `categories`,
`segments`, `order-types`, `product-prices`, `users`.

Detalhe individual: **somente** `customers`, `products`, `orders`. Os demais
respondem 404.

### Envelope de listagem

```json
{"resource": "...", "count": 0, "pageCursor": "...", "nextCursor": "...", "data": []}
```

`nextCursor` só vem preenchido quando existe próxima página. O consumidor só pode
gravar o cursor **depois** de persistir toda a página.

### Erros

```json
{"error": "...", "details": {...}}
```

429 traz `Retry-After` e `details.tempo_ate_permitir_novamente`.

---

## 2. Customer document lookup: incremental local index

Evidence: the official [Mercos customer API](https://docs.mercos.com/reference/v1clientes)
documents `cnpj` (CPF for individuals, digits only) and only `excluido` as a
customer GET filter. The adaptor accepts only `alterado_apos` on
`GET /v1/customers`. **There is no lookup by CPF/CNPJ** — neither in Mercos
(documented) nor in the adaptor — so none is called. The agent keeps its own
index (`app/commerce/mercos/customer_index.py`, migration 025).

Index. `run_customer_sync` consumes the adaptor list envelope and stores, per
tenant, an HMAC-SHA-256 digest of each valid 11/14-digit document
(`CUSTOMER_DOCUMENT_HMAC_KEY`, >= 32 chars, never stored; only a key
fingerprint is). No document text, no payload. `excluido: true` (documented
field) removes the customer; absent/`null` keeps it. Alphanumeric CNPJ
(documented by Mercos) is not indexed — and the agent's validator does not
accept it either, so it can never produce a false NOT_FOUND.

Lookup contract (`lookup_customer_by_document`):

| status | meaning | registration |
| --- | --- | --- |
| `FOUND` | exactly one customer (or a local created-claim) | link, zero POST |
| `NOT_FOUND` | complete, recent baseline and no match | may create — decided again under the lock |
| `AMBIGUOUS` | same document on 2+ customers (legacy duplicates) | handoff, zero POST |
| `CREATION_PENDING` | a creation for this document is in flight or unknown | handoff, zero POST |
| `INDEX_NOT_READY` | never synced, baseline in progress, key rotated, last sync failed, or stale (> 1 h) | registration unavailable |
| `PROVIDER_UNAVAILABLE` / `TIMEOUT` / `INVALID_RESPONSE` | database failure / timeout / invalid input | handoff, zero POST |

Baseline. `NOT_FOUND` is only trusted after a FULL baseline: a sweep that
started from an empty cursor with the current key and reached the end
(`ai_mercos_customer_index_config.baseline_completed_at`). A leftover
incremental cursor is never taken as a baseline — the first run resets it.
A baseline may span several runs (page budget); until it completes, lookups
answer `INDEX_NOT_READY`.

One POST per document. `create_customer` takes a transaction-scoped advisory
lock on `tenant + digest` (same pattern as `app/db.py`/outbox), re-runs the
lookup inside it and inserts a durable claim
(`ai_mercos_customer_creation_claim`) before POSTing. Another conversation or
worker confirming the same document finds the claim (`CREATION_PENDING`, or
`FOUND` once created) and never POSTs. Outcome: `created` (id kept, `FOUND`
until the sync sees it), `unknown` (transport/5xx/unreadable body: keeps
blocking until the team verifies), or released (definitive 4xx/429: nothing
was created).

Key rotation. A different `CUSTOMER_DOCUMENT_HMAC_KEY` makes the index
`INDEX_NOT_READY` immediately (never `NOT_FOUND`). The next sync drops the
tenant's digests, resets the customer cursor and starts a new baseline;
registration returns only when it completes. Rotation is refused while there
are unresolved (`creating`/`unknown`) claims — resolve them first. No
dual-key period is supported.

Operations: apply migration 025, set the key, run
`POST /api/cron/commerce/sync/customers` (or the admin route) until the
baseline completes, then schedule it well inside the 1 h freshness window.

Create payload (each field and its evidence):

| field | evidence |
| --- | --- |
| `tipo` (`F`/`J`) | Mercos docs: allowed values `J`, `F` |
| `razao_social` | Mercos docs: legal name, or the person's name for PF |
| `nome_fantasia` | Mercos docs |
| `cnpj` (digits only) | Mercos docs: CNPJ for PJ, CPF for PF, no punctuation |
| `emails: [{"email": ...}]` | Mercos docs: list of Email objects; JSON example uses `email` (field table labels it `e-mail`) — **confirm in homologation** |
| `telefones: [{"numero": ...}]` | Mercos docs |
| `observacao` | Mercos docs (String 500) |

`ativo` is not sent. The created id: the adaptor now returns
`{"id": <MeusPedidosID header>}` on writes (evidence: the previous backend read
that header; not described in the Mercos customer page) — **confirm in
homologation** before enabling `MERCOS_CUSTOMER_MUTATIONS_ENABLED`.

Limits. The index is a snapshot plus local claims: a customer created in
Mercos by someone else after the last sync is invisible until the next sync.

## 4. Política de freshness

`app/commerce/mercos/freshness.py`. Fora da janela o fato vira `unconfirmed` —
nunca `zero`, nunca `indisponível`.

| Tipo | Janela | Razão |
| --- | --- | --- |
| identidade | 7 dias | nome/referência mudam pouco |
| preço | 12 horas | muda com frequência |
| estoque | 1 hora | muda o tempo todo |

Tipo desconhecido cai na janela mais curta: na dúvida, exigir confirmação.

---

## 5. Segurança

- Apenas `MERCOS_ADAPTOR_URL` e `MERCOS_ADAPTOR_API_KEY` (+ timeout) existem no XNamai.
- `ApplicationToken`, `CompanyToken` e `MERCOS_BASE_URL` são do adaptador.
- Mutação nunca é repetida; falha ambígua vira `mutation_state=unknown`.
- `sanitize()` remove chave/token recursivamente antes de qualquer log.
