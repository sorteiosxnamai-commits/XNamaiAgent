# Taxonomia de intenção do XNamaiAgent

Levantada do código real deste repositório, não de outro projeto. Fronteira única da intenção de venda:
`app/sales/intent_router.py`. Comportamento congelado em
`tests/test_intent_characterization.py`; fronteira e contrato em
`tests/test_intent_router.py`.

## Três camadas — só uma é "intent de venda"

| Camada | Quem decide | Valores | Consumidor |
| --- | --- | --- | --- |
| **Domínio** da conversa | interpretador LLM (`SalesInterpretation.domain`); sem LLM, `intent_router.domain_from_text` | `commerce`, `store_general`, `greeting`, `out_of_scope` | `openai_agent` (escopo do turno) |
| **Intent de venda** | `intent_router.intent_from_interpretation` (com LLM) / `intent_from_text` (sem LLM) | tabela abaixo | `sales_agent` (plano, esclarecimento, acao comercial) |
| Sinal por palavra-chave | `context_builder.detect_primary_intent` | `human_support`, `commerce`, `general` | `openai_agent`: fatos do caminho legado e guarda de privacidade — **não roteia** venda |

Fora do router, por decisão: o adaptador `turn_understanding` (schema
alternativo do interpretador, atrás de flag) e o `CommerceTurnResolution`
(`commerce/turn_resolver`), que resolve **ações de continuidade** (posição na
lista, carrinho, detalhe do item ativo) a partir de estado — é fase de diálogo,
não classificação de intenção (ver próximas extrações).

## Intent de venda

| Intent | Significado | Ação comercial | Goal (sem LLM) |
| --- | --- | --- | --- |
| `clarification` | perguntar antes de buscar | nenhuma | `discover` |
| `product_search` | achar produto/categoria | `product_search` | `find` |
| `recommendation` | buscar para recomendar dentro de restrições | `product_search` | `recommend` |
| `product_comparison` | comparar produtos citados | `product_search` | `compare` |
| `price` | preço / condição de pagamento | `product_price` | `inspect` |
| `inventory` | estoque / disponibilidade | `product_inventory` | `inspect` |
| `coupon` | cupom | `coupon_search` | `inspect` |
| `purchase_intent` | quer comprar (só do fallback por palavra-chave) | nenhuma sozinha | `buy` |

### Do interpretador para intent

1. `goal` ∈ {discover, recommend, buy} **e** sinal de busca
   (`enough_information_to_search` / `ready_for_retrieval` / `stop_clarification`) → `recommendation`;
2. senão, `needs_clarification` → `clarification`;
3. senão, por `goal`: discover/buy/after_sales → `clarification`; find → `product_search`;
   recommend → `recommendation`; compare → `product_comparison`; inspect →
   `inventory` | `coupon` | `price` | `product_search` conforme `information_needed`.

### Roteamento

`route_sales_intent(intent, force_retrieval=...)`: quando o cliente pediu para
agir (`force_retrieval`), `clarification` vira `recommendation`
(`forced_retrieval=True`). Intent fora da taxonomia → `None` (o turno não é
roteado para o catálogo).

## Quirks preservados (caminho sem LLM)

Congelados pela characterization, não corrigidos nesta extração:
"tem cupom de desconto?" → `product_search`; "compara X e Y" → `out_of_scope`;
"quero falar com um atendente" → domínio `commerce` (o handoff vem do sinal
`human_support`). Com LLM essas mensagens vão pelo interpretador.

## Removido com a extração

- `sales_agent._normalize_semantic_plan` e `_parse_scope`: terceira tabela
  goal↔intent, sem chamador (código morto);
- `sales_agent._parse_plan`: lista de intents de um planner antigo, sem chamador;
- `_ACTION_TO_PLAN`, a tabela intent→ação inline de `handle_sales_message` e o
  override de `force_retrieval`: agora no router.

Compatibilidade mantida (pequena, testada em `test_intent_router.py`):
`commerce_router.resolve_commerce_action` e `sales_agent._is_greeting`
continuam existindo e delegam ao router — são pontos de monkeypatch de testes e
de import de `openai_agent`.
