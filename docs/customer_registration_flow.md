# Cadastro de clientes e XNaMai Club — fluxo determinístico

Cadastro é **fluxo de código**. A persona controla apenas comunicação e
identidade; o modelo nunca inicia, decide, narra nem inventa um cadastro.

## Roteamento (antes de qualquer LLM)

`app/account_flows.py::handle_account_flows` roda logo após o carregamento do
estado comercial em `openai_agent.generate_agent_reply_async` (antes de
retomada de saudação, recuperação de pedido, story, imagem e interpretação) e
também no início de `sales_agent._handle_sales_message_inner`:

1. pergunta sobre o Club (`club_xnamai.is_club_request`) → resposta do Club;
2. cadastro pendente (`awaiting_customer_registration_data|confirmation`) → fluxo;
3. pedido novo reconhecido pelo Intent Router
   (`intent_router.is_customer_registration_request`, intent `customer_registration`).

A detecção é gramatical (radical `cadastr-` + verbo de pedido/criação, ou a
palavra sozinha), não uma lista de frases. Não são pedidos novos: negação,
"já tenho", atualização/alteração, cancelamento, produto cadastrado e Club.

## Estado

- PII do cadastro fica **somente** em `CommerceConversationState.customer_registration`
  (`status`, `draft`, `sources`, `customer_id`). Nunca em Qualification Slots,
  Dialogue Phase, memória ou logs (`redact_value` mascara `document`, e-mail,
  telefone, razão social e nome fantasia).
- `pending_action` persiste até conclusão, cancelamento, handoff ou erro
  terminal. Uma pergunta comercial no meio do cadastro é respondida
  normalmente e **não** limpa a pendência; saudação não rouba o turno.
- Status terminais `unknown`, `lookup_failed` e `created_pending_sync` bloqueiam nova criação
  automática na conversa (a equipe verifica).

## Coleta

- Parser natural + "Campo: valor": e-mail por padrão; CPF/CNPJ só com dígito
  verificador válido (ou com "cpf"/"cnpj" explícito, para apontar o erro);
  telefone só quando anunciado ou formatado; nome também é aceito como resposta
  direta à pergunta de nome. PF/PJ é inferido do documento, inclusive CNPJ
  alfanumérico validado pelo DV oficial.
- Dados do canal: `sender_phone` válido preenche o telefone (não é pedido de
  novo); `sender_name` não vira nome legal. Precedência: valor explícito do
  turno > rascunho > canal. O cliente sempre pode corrigir.
- Pede **apenas** o que falta ou está inválido.
- Revisão mascarada (documento `***1234`, e-mail `ab***@dominio`, telefone
  `***1234`) e confirmação explícita ("confirmo o cadastro").

## Capacidade e duplicidade

O fluxo de coleta está disponível antes das capacidades de criação. A confirmação
só executa mutation quando o runtime expõe **as duas** capacidades:

| capacidade | origem | hoje |
|---|---|---|
| `create_customer` | `MERCOS_CUSTOMER_MUTATIONS_ENABLED=true` (nunca ligado automaticamente) | desligado por padrão |
| `lookup_customer_by_document` | índice local de HMAC dos documentos (sync incremental do MercosAdaptor, migration 025) | pronto só após baseline completo e recente |

Sem elas, a coleta e revisão continuam; na confirmação, o pedido recebe
resposta determinística de indisponibilidade e handoff, sem POST.

Após a confirmação: `lookup_customer_by_document` → `FOUND`: vincula, sem
criar; `NOT_FOUND`: `create_customer`, que refaz a busca sob trava por
documento e só então faz **um** POST; `AMBIGUOUS`, `CREATION_PENDING`,
índice não pronto ou falha: handoff, nada é criado. Resultado de criação
desconhecido (`mutation_state_unknown`, exceção de transporte): sem retry, handoff,
status `unknown`, e o documento fica bloqueado até a equipe verificar. O id
criado é gravado em `mercos_customer_id`; uma segunda confirmação — nesta ou
em outra conversa — não cria outro. Resposta 2xx sem ID vira
`created_pending_sync`: o sync resolve pelo digest e vincula sem segundo POST.
Erro 422 de obrigatoriedade libera o claim e pede o campo faltante, com nova
revisão e confirmação. Detalhes em `docs/mercos_chat_registration.md`.

Não existe busca por CPF/CNPJ no MercosAdaptor nem na API Mercos documentada;
detalhes do índice, estados e operação em `docs/mercos_contract.md` §2.

## XNaMai Club

O Club só explica, reconhece membro/não membro e direciona ao site oficial —
não há capacidade de criar assinatura. As URLs públicas oficiais vêm de uma
fonte única, `site_knowledge.official_public_urls()` (site, catálogo, Club),
consumida pela validação factual. Caminhos de produto, checkout e pagamento
nesses domínios continuam exigindo evidência comercial do turno.
