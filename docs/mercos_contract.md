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

## 2. ⛔ Data dictionary — BLOQUEADO

```
MERCOS_SCHEMA_DISCOVERY_BLOCKED = true
```

O adaptador é um **pass-through sem esquema**: repassa o corpo devolvido pela
Mercos dentro de `data` sem conhecer os campos de negócio. Busquei nas cinco
fontes, nesta ordem:

| # | Fonte | Resultado |
| --- | --- | --- |
| 1 | fixtures do MercosAdaptor | não existem |
| 2 | testes do MercosAdaptor | só `id`, `ultima_alteracao`, `itens[].id`, `itens[].produto_id` |
| 3 | fixtures/logs sanitizados no XNamai | não existem |
| 4 | documentação do projeto | nenhuma descreve campos Mercos |
| 5 | ambiente MercosAdaptor acessível | `MERCOS_ADAPTOR_URL` e `MERCOS_ADAPTOR_API_KEY` **não configurados** |

Sem nenhuma dessas fontes, escrever o mapeamento seria **inventar nomes de
campo** — e um preço ou estoque inventado chega ao cliente como fato oficial.

### Campos comprovados (únicos que podem ser usados hoje)

| Campo | Tipo | Significado | Nullable | Usado pelo XNamai |
| --- | --- | --- | --- | --- |
| `id` | int/str | identidade do registro | não | ✅ `external_id` |
| `ultima_alteracao` | str ISO | marca de alteração incremental | sim | ✅ cursor e `changed_at` |
| `itens[].id` | int | id do item do pedido | — | ⏳ pendente |
| `itens[].produto_id` | int | produto referenciado | — | ⏳ pendente |

### PRODUCT — necessário e ausente

| Campo pretendido | Status |
| --- | --- |
| nome / descrição | `UNKNOWN` |
| referência / código | `UNKNOWN` |
| EAN | `UNKNOWN` |
| preço | `UNKNOWN` |
| estoque | `UNKNOWN` |
| ativo / disponível | `UNKNOWN` |
| marca / categoria | `UNKNOWN` |
| URL pública | `UNKNOWN` (provavelmente inexistente — Mercos é B2B) |

### CUSTOMER — necessário e ausente

| Campo pretendido | Status |
| --- | --- |
| razão social / nome | `UNKNOWN` |
| CPF / CNPJ | `UNKNOWN` |
| e-mail / telefone | `UNKNOWN` |
| endereço | `UNKNOWN` |

### ORDER — necessário e ausente

| Campo pretendido | Status |
| --- | --- |
| cliente | `UNKNOWN` (só `produto_id` dentro de `itens`) |
| status | `UNKNOWN` |
| totais | `UNKNOWN` |
| itens: quantidade/preço | `UNKNOWN` |

**Nenhuma semântica foi presumida.** Campo ambíguo está marcado `UNKNOWN`.

### O que destrava

Uma linha **anonimizada** de `products`, `customers` e `orders` — só nomes de
campo, tipos e estrutura, sem nome real, documento, e-mail, telefone ou endereço.
Alternativa: a documentação de campos da Mercos v1/v2. Ou credenciais de um
MercosAdaptor de sandbox para descoberta read-only.

---

## 3. Limitações estruturais (independentes do schema)

Estas não se resolvem com amostra de payload — são do contrato:

| Limitação | Consequência |
| --- | --- |
| listagem aceita **só** `alterado_apos` | não há busca textual → `search_products` depende de índice local |
| sem filtro por documento | `search_customer` inviável sem sync local de clientes |
| `GET /{resource}/{id}` pode dar 401/403 em produção | revalidação por id não é confiável; o próprio adaptador avisa: *"GET por ID só existe no sandbox"* |
| `GET /v1/orders` é incremental global | não responde "os pedidos deste cliente" |
| sem carrinho | `get_cart`, `create_cart`, `set_cart_item_quantity`, `delete_cart` → UNSUPPORTED |
| sem cupom | `list_coupons`, `get_coupon` → UNSUPPORTED |
| `carriers` ≠ cotação | `quote_shipping`, `list_shipping_methods` → UNSUPPORTED |
| `payment-conditions` ≠ link de pagamento | `get_order_payment` → UNSUPPORTED |

```
MERCOS_ADAPTOR_GAP:
  - busca textual de produtos           (mitigável por índice local)
  - busca de cliente por CPF/CNPJ/email (precisa de endpoint ou sync local)
  - detalhe por id confiável em produção
  - payload documentado de POST /v1/orders
```

---

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
