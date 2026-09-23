# Evolução arquitetural — XNamaiAgent × NSAgentForSorteios

Referência: NSAgentForSorteios `origin/main` @ `4ee3c22` (lido em cópia
isolada; nada importado). Portamos **arquitetura, padrões, testes, isolamento e
confiabilidade** — nunca domínio (NewStore, sorteios, rifas, Tray), persona
antiga, regras de campanha ou regras comerciais daquele produto.

## 1. Comparação de capacidades

| Recurso NSAgent | Problema que resolveu | Existe no XNamai? | Decisão | Adaptação necessária |
| --- | --- | --- | --- | --- |
| `sales/intent_router.py` | flags de roteamento de venda espalhadas pelo handler | Não — decisões espalhadas em `sales_agent` + 2 cópias mortas | **PORTADO ADAPTADO** (esta rodada) | taxonomia derivada do XNamai; sem `route_kind` (ver §3) |
| `sales/dialogue_phase.py` | fase autoritativa discovery→shortlist→buy→checkout; reset de navegação | Parcial: `purchase_stage` (11 valores, vem do LLM), `pending_action`, `order_confirmation_status` | PORTAR ADAPTADO | fase derivada do **estado**, não do LLM; ver §4.1 |
| `sales/qualification_slots.py` | loop de perguntas (nome/cidade), repetição, apagar contexto | Parcial: `DISCOVERY_STATE` (known/explicit_no_preferences/recent_questions) | PORTAR ADAPTADO | slots do catálogo Xnamai, sem nome/cidade de persona; §4.2 |
| `sales/scope_send_gate.py` | lista enviada violando marca excluída/escopo pedido | Não | PORTAR ADAPTADO | começar só com restrições explícitas do turno; §4.3 |
| `sales/conversation_repair.py` | cliente diz "não foi isso" | Sim: `commerce/conversation_repair.py` (+ política publicada) | JÁ EXISTE | estender aos casos de §4.4 |
| `memory/contact_preference_memory.py` | reaproveitar marca/estilo/orçamento no próximo contato | Parcial: propostas de memória + `user_preferences` + working memory | PORTAR ADAPTADO | separar episódico/preferência/fato/operacional; §4.5 |
| `verify/final_response.py` | validar o texto real enviado e derivar memória do que foi entregue | Parcial: `factual_validator` + presenter + composer, em ordem implícita | PORTAR ADAPTADO | pipeline explícito; §4.6 |
| `verify/outbound_compliance.py` | lista de preferência sem aderência; links mortos | Parcial: validação factual (fatos) | PORTAR ADAPTADO | compliance de aderência às restrições; sem LLM |
| `ops/handoff_queue.py` | marcar a conversa certa para atendimento humano | Parcial: `handoff_service` + `human_takeover` (pausa) | PORTAR ADAPTADO (depois) | depende do modelo de `conversas` do ChatBô; não iniciado |
| `sales/policies/tool_policy.py` | gate antes de mutação no provider | Equivalente: mutações não expostas (`LLM_EXPOSED_CAPABILITIES`), `create_order` desabilitado | JÁ EXISTE (por ausência de mutação) | revisitar quando o Mercos ganhar escrita |
| `sales/policies/action_authority.py`, `confirmation.py` | autoridade de ação/confirmação | Sim: `app/sales/policies/` | JÁ EXISTE | — |
| `sales/policies/objection_authority.py` | respostas fixas a objeções (preço, prazo, desconto) | Não | **NÃO PORTAR** | é estilo de venda/persuasão — pertence à persona do GPT |
| `sales/turn_contract.py` + `answer_council.py` | mensagem × memória em conflito; checagem dupla com um restart | Parcial: `commerce/turn_resolver`, crítico, validação factual | PORTAR ADAPTADO (depois de Scope Gate) | reduzir: o conselho de 1.2k linhas é específico do catálogo NewStore |
| `evaluation/regression_*` | regressão de conversas reais com juiz LLM e repositório | Parcial: evals offline determinísticos (89) | PORTAR ADAPTADO (depois) | manter offline como gate; juiz LLM só opcional |
| retrieval (`catalog/retrieval`, budget) | ranking e orçamento de chamadas de busca | Sim: `product_retrieval`, `catalog_index`, budget de LLM | JÁ EXISTE | — |
| evidência visual | foto ≠ produto ≠ fato | Sim (rodada anterior, 4 camadas) | JÁ EXISTE | — |
| resultados estruturados | "provider caiu" ≠ "não existe" | Sim (`commerce/result_status`) | JÁ EXISTE | — |
| salvaguardas de learning | aprendizado não vira persona sozinho | Sim (propostas `pending_review`, nunca auto-ativa) | JÁ EXISTE | — |
| `tray/*`, `door_*`, campanhas | integração e domínio NewStore | — | **NÃO PORTAR** | — |

## 2. Intent Router (feito)

Ver `docs/intent_taxonomy.md`. Recebe interpretação ou texto; devolve
`IntentResult(intent, forced_retrieval)` com `commerce_action`. Não responde,
não chama provider, não escreve memória, não decide persona.

## 3. O que do intent router do NSAgent **não** entrou

`route_kind` (browse/refine/reject/close/inspect/talk/qualify) e as flags de
compra (`purchase_close`, `purchase_close_hold`, `skip_catalog_fanout`) dependem
de fase de diálogo e slots de qualificação que o XNamai ainda não tem como
módulos. Portá-los agora seria criar intents especulativos. Entram junto com
Dialogue Phase (§4.1), que é quem sabe "fechamento" vs "navegação".

## 4. Próximas extrações (desenho, não implementado)

### 4.1 Dialogue Phase — próximo passo

- **Problema**: a etapa da conversa vem de três fontes sem autoridade:
  `interpretation.purchase_stage` (LLM, 11 valores), `state.pending_action` e
  `order_confirmation_status`. O handler decide com `if` sobre as três.
- **Fases reais do XNamai** (derivadas do estado, não copiadas):
  `discovery` (sem produto apresentado) → `shortlist` (`last_presented_products`)
  → `product_focus` (`active_product`) → `cart` (itens no carrinho) →
  `order_review` (`order_review_version`, confirmação pendente) →
  `after_sale` (`order_id`) ; ortogonal: `handoff` (takeover humano ativo).
  Não há `negotiation` (sem desconto/negociação no produto) nem `conversion`
  distinta de `order_review`.
- **Responsabilidade**: dado estado + intent, dizer a fase e as transições
  permitidas (ex.: reset para `discovery` quando o cliente muda de assunto).
- **Inputs**: `CommerceConversationState`, `IntentResult`, texto (reset).
- **Outputs**: `DialoguePhase` + flags (`blocks_qualification`, `resets_browse`).
- **Dependências**: nenhuma de provider; só estado.
- **Código que migra**: `apply_commerce_domain_context`, cálculo de
  `purchase_stage` em `evolve_commerce_state`, ramos de confirmação no início de
  `_handle_sales_message_inner`.
- **Referência NSAgent**: `commerce_dialogue_phase`, `message_resets_dialogue_to_discovery`,
  `session_in_checkout_phase` (conceito; lista de fases diferente).

### 4.2 Qualification Slots

- **Problema**: `_discovery_state` mistura contagem de perguntas, preferências
  conhecidas e "cliente pediu para agir".
- **Slots reais**: categoria (`product_type`), produto (`model/reference/ean`),
  marca, orçamento (`budget_min/max`), atributos (`attributes`: conector,
  potência…), cor, material/estilo, quantidade, compatibilidade (modelo do
  aparelho), ocasião/destinatário (existem no schema). Contato: CEP/dados de
  checkout já têm serviço próprio (`checkout_data_service`) e ficam fora.
- **Estados**: `UNKNOWN` / `KNOWN` / `NOT_APPLICABLE` / `REFUSED`
  (`REFUSED` = `explicit_no_preferences`, que já existe).
- **Inputs/outputs**: interpretação + turnos recentes → mapa de slots + "o que
  perguntar a seguir" (no máximo 2, regra técnica já existente).
- **Código que migra**: `_discovery_state`, `_known_preferences`,
  `_subject_identifiable`, `_needs_clarification_before_retrieval`.
- **Referência NSAgent**: `qualification_slots.py` (mecânica de rehidratação e
  "slot já respondido"); nome/cidade do NSAgent **não** entram.

### 4.3 Scope Send Gate

- **Problema**: nada impede enviar uma lista que ignora uma restrição explícita
  do turno (marca pedida/excluída, orçamento).
- **Decisão futura**: `READY` / `NEEDS_CONTEXT` / `PROVIDER_BLOCKED` / `NEEDS_HUMAN`.
  `PROVIDER_BLOCKED` reutiliza `CommerceResultStatus` (provider fora/timeout).
- **Inputs**: `AgentResult` (produtos apresentados), slots conhecidos, status comercial.
- **Outputs**: decisão + produtos filtrados ou pedido de contexto.
- **Dependências**: slots (4.2) e status comercial; sem LLM.
- **Referência NSAgent**: `validate_scope_send_gate` (marca excluída, relist no fechamento).

### 4.4 Conversation Repair

Já existe para "não foi isso". Situações a cobrir:

| Situação | Hoje | Futuro |
| --- | --- | --- |
| cliente corrige informação | interpretador substitui preferência | slot passa a `KNOWN` com a correção; registrar conflito |
| memória conflita com a mensagem | mensagem vence implicitamente | regra explícita "mensagem vence; memória vira stale" |
| provider mudou preço/estoque | revalidação + validação factual | avisar a mudança em vez de repetir o valor antigo |
| tool falhou | status estruturado + mensagem operacional | manter; registrar no estado para não prometer |
| estado inválido | `from_payload` volta a estado vazio | reparo parcial em vez de descarte total |
| resposta anterior incompatível | crítico (risco) | reparo determinístico citando o fato novo |

### 4.5 Contact Preference Memory

Separação futura (persona **não** entra):

| Tipo | Exemplo | Onde vive hoje | Validade |
| --- | --- | --- | --- |
| episódico | "perguntou de capa ontem" | `recent_topics`, resumo de conversa | curta |
| preferência | "prefere USB-C", marca preferida | propostas de memória (`ai_memory_proposals`) | longa, revisável |
| fato | nome preferido, CNPJ informado | `user_preferences`, dados de checkout | até correção |
| estado operacional | carrinho, pedido, pending_action | `commerce_state` | da sessão |

Referência NSAgent: `contact_preference_memory` (persistir a partir da
interpretação, rehidratar só campos vazios, mensagem atual sempre vence).

### 4.6 Final Response / Compliance

Pipeline alvo, com cada etapa sendo uma função pura testável:

```
FACTS (provider, status estruturado, evidência visual)
  → GPT RESPONSE (persona publicada, única fonte de voz)
  → FACTUAL VALIDATION (factual_validator — já existe)
  → COMPLIANCE (aderência às restrições do turno; Scope Gate)
  → CHANNEL FORMAT (presenter thin — já existe)
  → OUTBOUND (outbox — já existe)
```

Hoje as etapas existem mas a ordem está implícita em `message_pipeline`
(estágios `agent_decision` → `response_critique` → `enrich_result`). Extrair
para um `finalize_response` explícito, sem persona duplicada. Referência
NSAgent: `verify/final_response.finalize_response`, `outbound_compliance`.

## 5. Ordem recomendada

1. Dialogue Phase (autoridade de fase; destrava `route_kind`)
2. Qualification Slots
3. Scope Send Gate + Final Response/Compliance
4. Conversation Repair (estender)
5. Contact Preference Memory
6. Handoff queue e regressão (dependem de decisões de produto/infra)
