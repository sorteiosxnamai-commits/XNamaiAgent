# Dialogue Phase e Qualification Slots

| Pergunta | Módulo |
| --- | --- |
| "O que o cliente quer agora?" | `app/sales/intent_router.py` |
| "Em que etapa da conversa comercial estamos?" | `app/sales/dialogue_phase.py` |
| "O que já sabemos para avançar?" | `app/sales/qualification_slots.py` |
| "O que devemos dizer / perguntar?" | **nenhum dos três** — persona (voz) e, no futuro, Scope Send Gate (decisão) |

Fluxo: `MESSAGE → INTENT ROUTER → DIALOGUE PHASE → QUALIFICATION`. O router
não conhece fase nem slots; fase e slots não importam um ao outro; nenhum
depende de provider, canal, outbox, memória persistida ou persona (testes
estruturais em `tests/test_dialogue_phase.py`).

## 1. Mapa do estado anterior

| Sinal atual | Significado | Dono atual | Dono futuro |
| --- | --- | --- | --- |
| `last_presented_products` | opções mostradas | `evolve_commerce_state` | lido por **Dialogue Phase** (`shortlist`) |
| `active_product` / `previous_active_product` | produto em foco | resolução de referência (`resolve_commerce_reference`) | lido por **Dialogue Phase** (`product_focus`) |
| `cart_items`, `cart_session_id`, `cart_*` | carrinho | `cart_service` | lido por **Dialogue Phase** (`cart`) |
| `pending_action` (12 valores) | próxima etapa oferecida (checkout, confirmação, pagamento) | `sales_agent` / serviços | lido por **Dialogue Phase** (`checkout`, `order_review`, `after_sale`) |
| `purchase_stage` (string livre) | estágio declarado por LLM (`SalesInterpretation`) **e** por serviços (`cart_service`, `payment_service`, `order_context_recovery`) | 4 produtores sem autoridade | sinal de entrada da **Dialogue Phase**; unificar produtores é follow-up |
| `order_confirmation_status`, `order_review_version` | revisão aguardando confirmação | `turn_flow` / `sales_agent` | **Dialogue Phase** (`order_review`) |
| `order_id`, `order_lookup_id`, `order_payment_*`, `order_status_group` | pedido existe | `order_service` / recuperação de pedido | **Dialogue Phase** (`after_sale`) |
| `pending_commerce_action`, `last_commerce_action`, `last_requested_fact` | continuidade conversacional curta | `commerce/turn_flow` | fica (continuidade, não fase) |
| `product_resolution_state` | resultado da identificação (found/plausible) | resolução/visão | fica (resolução, não fase) |
| `remarketing._remarketing_stage` | estágio para escolher texto de remarketing | `remarketing` | **duplicação de fase** — migrar para Dialogue Phase é follow-up (a taxonomia do remarketing difere: agrupa revisão em `checkout`) |
| `context_resume.commerce_state_resumable_score` | prioridade do estado para retomar/mesclar | `context_resume` | fica (mescla de estados, não fase) |
| `active_preferences` | preferências do turno anterior | `evolve_commerce_state` | lido por **Qualification** (`CONVERSATION_STATE`) |
| `interpretation.subject/preferences/quantity/payment_method_preference` | o que o turno diz | interpretador | **Qualification** (`USER_EXPLICIT`/`USER_INFERRED`) |
| `explicit_no_preferences` | "tanto faz"/"sem preferência" | interpretador | **Qualification** (`REFUSED`) |
| `_known_preferences`, `_subject_identifiable` (em `sales_agent`) | "o que sabemos" do discovery | `sales_agent` | **movido** para Qualification |
| `clarification_count`, `recent_questions`, `force_retrieval`, `_needs_clarification_before_retrieval` | decidir perguntar ou buscar | `sales_agent` | **fica** — futuro Scope Send Gate |
| `human_takeover_active`, `result.handoff_required` | humano no atendimento | `human_takeover` / handoff | flag ortogonal `handoff_active` |

## 2. Dialogue Phase

Fases validadas contra o estado real:

| Fase | Prova no estado |
| --- | --- |
| `discovery` | nada apresentado/selecionado |
| `shortlist` | `last_presented_products` |
| `product_focus` | `active_product` |
| `cart` | `cart_items` / `cart_session_id` / `purchase_stage=cart_created` |
| `checkout` | `pending_action` ∈ {choose_checkout_channel, awaiting_shipping_zipcode, awaiting_shipping_selection, awaiting_checkout_data} ou `purchase_stage` ∈ {checkout_channel_selection, shipping, checkout_ready} |
| `order_review` | `order_confirmation_status=pending` / `order_review_version` / `pending_action` ∈ {awaiting_order_confirmation, awaiting_order_customer_document} |
| `after_sale` | `order_id` / `order_lookup_id` / `pending_action=awaiting_payment` / `purchase_stage` ∈ {awaiting_payment, payment_confirmed, after_sales} |

**`checkout` foi acrescentada** à lista proposta: existe no código (coleta de
canal, frete e dados antes da revisão) e o remarketing já a trata como etapa.
Não existem `negotiation` nem `conversion` separadas (não há negociação de preço;
conversão é `order_review`). `handoff` não é fase: `DialogueState.handoff_active`.

Precedência de leitura: `after_sale > order_review > checkout > cart >
product_focus > shortlist > discovery`.

Transições por evidência do turno (forward vem do estado persistido):

1. `shortlist → product_focus`: referência a item apresentado (posição, nome, "esse", recomendação);
2. `shortlist/product_focus → discovery`: busca nova independente, mudança explícita de assunto, "começar de novo", "nenhuma dessas";
3. `cart → product_focus|discovery`: remover o único item;
4. `after_sale` + turno de produto (não sobre o pedido): nova venda — lê só os campos de navegação.

`cart/checkout/order_review` não regridem por navegação (venda aberta). Turno
não-comercial não move a fase. Preço, sozinho, não define fase.

Entradas: estado anterior, `IntentResult`, interpretação, texto, `handoff_active`.
Saída: `DialogueState(phase, state_phase, handoff_active)`.

## 3. Qualification Slots

Slots derivados do interpretador XNamai: `category`, `product` (referência/EAN),
`brand`, `model`, `budget` ({min,max}), `quantity`, `color`, `style`, `material`,
`occasion`, `recipient`, `attributes` (conector, potência…), `payment_method`.
Nenhum obrigatório. **Não há** `urgency` nem contato: o interpretador não os
produz, e dados de checkout têm serviço próprio.

| Estado | Significado |
| --- | --- |
| `UNKNOWN` | ainda não dito — ausência nunca é recusa |
| `KNOWN` | valor dado |
| `REFUSED` | o cliente declinou explicitamente (`explicit_no_preferences`: "tanto faz a cor") — não perguntar de novo |
| `NOT_APPLICABLE` | não faz sentido no fluxo (turno não-comercial; preferências em pós-venda) |

Origem: `USER_EXPLICIT` (dito nesta mensagem, ou marcado como correção) >
`USER_INFERRED` (deste turno, trazido do contexto pelo interpretador) >
`CONVERSATION_STATE` (turno anterior) > `MEMORY`.

Regras: o que este turno diz (conhecido, recusado ou não-aplicável) vence;
lacunas são preenchidas pela origem mais alta; negação explícita de um valor
antigo ("não quero MarcaB", "sem preto") o descarta; marcadores de correção
("na verdade", "quis dizer", "mudei de ideia", "me enganei") tornam o valor
atual explícito. Valor explícito vence recusa no mesmo turno.

Qualification **não** decide perguntas. `DISCOVERY_STATE` (vai ao prompt) segue
idêntico: `known_preferences` e `subject_identifiable` vêm dos slots crus do
turno; a lista `explicit_no_preferences` continua a do interpretador
(characterization: `tests/test_discovery_state_characterization.py`).

Integração nesta rodada: **somente observação** — `response_metadata["dialogue"]`
com fase, fase do estado, handoff e resumo dos slots (status/origem, sem valores).
Nenhuma decisão usa isso ainda.

## 4. Projeto de referência — o que foi usado

Comparação com o projeto de referência descrito em `docs/architecture_evolution.md`.

| Conceito de referência | Decisão | Motivo |
| --- | --- | --- |
| fase autoritativa discovery/shortlist/buy/checkout | PORTAR ADAPTADO | fases derivadas do estado XNamai (7, com `checkout`/`order_review`/`after_sale` separados) |
| fase **gravada** no estado (`state.dialogue_phase`) | NÃO PORTAR (agora) | derivar evita migração de estado e divergência entre produtores |
| reset por "começar de novo" / "nenhuma dessas" | PORTAR ADAPTADO | regex sem vocabulário de catálogo antigo |
| regexes de navegação com "relógio"/marcas | NÃO PORTAR | domínio do produto anterior |
| estado terminal de pedido / venda aberta vs navegação | PORTAR ADAPTADO | regra 4 (nova venda após pedido) e venda aberta não regride |
| `blocks_greeting_fast_path`, `blocks_farewell_fast_path` | NÃO PORTAR (agora) | são decisões de resposta — Scope Gate/persona |
| `apply_dialogue_phase_discovery_gates` | NÃO PORTAR (agora) | gate de esclarecimento = Scope Gate |
| slots de persona (nome, cidade, urgência, prefixo `qual:`) | NÃO PORTAR | qualificação de persona antiga; persona fica no GPT |
| detecção "cliente respondeu a pergunta anterior" | PORTAR ADAPTADO (futuro) | útil ao Scope Gate; aqui só a precedência/correção |
| reidratar slots de estado/memória | PORTAR ADAPTADO | `slots_from_preferences` + precedência explícita |
| `qualification_slots_sufficient` / `fulfillment_slots_ready` | NÃO PORTAR (agora) | "suficiente para agir" é o Scope Gate |

## 5. Promoção de orçamento LLM

- **Origem**: plano v6 ("promote without reset"): o orçamento nasce no início do
  turno, antes de se conhecerem sinais de risco (imagem, comparação,
  ambiguidade, retomada de checkout, falha de integração); a promoção seria
  aplicada no meio do turno.
- **Hoje**: `should_promote_to_complex` e `TurnRuntimeContext.promote_budget`
  têm testes unitários e **nenhum chamador**; todo turno nasce com orçamento 2.
- **Onde deveriam ser chamadas**: depois da interpretação (sinais imagem,
  comparação, ambiguidade) e antes do crítico em `message_pipeline`.
- **Decisão**: **manter por uma rodada**, sem ligar agora. A Dialogue Phase é
  um sinal natural (`checkout`/`order_review` = "checkout_resume"). Ligar junto
  com o Scope Send Gate, que é quem sabe se o turno precisa de chamada extra.
  Se não for ligada na próxima rodada, **remover** (código morto).

## 6. Próximo: Scope Send Gate (desenho, não implementado)

Entradas: `IntentResult`, `DialogueState`, `QualificationState`, `CommerceResultStatus`.

| Decisão | Quando |
| --- | --- |
| `NEEDS_HUMAN` | `handoff_active`, ou tentativas de reparo esgotadas (política publicada) |
| `PROVIDER_BLOCKED` | status comercial `provider_unavailable`/`timeout`/`invalid_response` |
| `NEEDS_CONTEXT` | intent de busca e `subject_identifiable=False`, ou intent com ação comercial sem produto identificável na fase; nunca pergunta slot `REFUSED`/`NOT_APPLICABLE`; limite de perguntas seguidas |
| `READY` | o resto |

Código que migra para ele: `_needs_clarification_before_retrieval`,
`force_retrieval`, `clarification_count`/`recent_questions`, o `vague_query`
de `handle_sales_message`. Também é o ponto de ligar a promoção de orçamento.

## 7. ROUTER FOLLOW-UP

- O interpretador com `goal=buy` vira intent `clarification` no router; fase e
  slots precisam ler `interpretation.goal` para distinguir "quero comprar este"
  de "estou descobrindo". A taxonomia não tem um intent de compra no caminho com
  LLM (só `purchase_intent`, do fallback por palavra-chave).
- Comportamentos congelados continuam congelados ("cupom" → `product_search`,
  "compara" → `out_of_scope`, "atendente" → `commerce`).
