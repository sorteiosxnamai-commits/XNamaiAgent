# Agente direto XNamai

`XNAMAI_AGENT_ENGINE=legacy|direct` seleciona o caminho na entrada de
`process_incoming_message`. O padrão é `legacy`. O agente direto usa Responses
com um único modelo responsável pela interpretação, consulta e resposta. Não
executa SalesInterpretation, council, critique ou reescrita semântica do legado.
Reutiliza filas, deduplicação, bloqueios, outbox e auditoria de entrega existentes.

## Configuração e ativação

```env
XNAMAI_AGENT_ENGINE=legacy
DIRECT_OPENAI_MODEL=
DIRECT_MAX_ROUNDS=4
DIRECT_TIMEOUT_SECONDS=65
```

Modelo vazio herda `OPENAI_MAIN_MODEL`, depois `OPENAI_MODEL`. A credencial é a
`OPENAI_API_KEY` já configurada na implantação. Não depende de `OPENAI_API_MODE`.
Não há fallback automático para o legado nem retries automáticos da OpenAI.

Requisitos: `CHATBO_WORKSPACE_ID`, banco operacional, persona ativa para esse
workspace/tenant/chave de persona e catálogo Mercos sincronizado para consultas
comerciais. Workspace ausente ou divergente e persona ausente/legada falham
explicitamente. Não há fallback para uma persona de outra empresa.

Publicar inicialmente com `legacy`. Testar o endpoint abaixo e validar conversas
reais de homologação antes de selecionar `direct` em uma nova implantação.
Rollback: restaurar `XNAMAI_AGENT_ENGINE=legacy`. A outbox já aceita continua
responsável pela entrega sem regenerar a resposta. `/api/health` informa o motor.

## Preview sem mensagens a clientes

`POST /api/test/direct`, com `Authorization: Bearer ADMIN_API_TOKEN`:

```json
{"workspace_id":"UUID_CONFIGURADO", "channel":"whatsapp", "text":"Preciso de um cabo USB-C"}
```

Retorna resposta, métricas e `session`. Reenviar `session` nos turnos seguintes.
A sessão é assinada, vinculada ao workspace e canal e expira em uma hora.
Não cria inbox/outbox, não envia mensagens nem altera conversas de clientes.
Pode ser usado enquanto produção usa `legacy`. O preview chama a OpenAI e tem
custo; testes pytest usam clientes simulados e bloqueiam tráfego real.

Com `ADMIN_API_TOKEN` no ambiente do terminal, também pode conversar pelo CLI:

```sh
python scripts/chat_direct.py --url https://SEU_AGENTE --workspace UUID_CONFIGURADO
```

Use `/reset` para uma sessão nova e `/sair` para encerrar. O script não imprime
token administrativo nem sessão assinada.

## Ferramentas e contexto

- `search_products`: busca paginada no índice alimentado pelo Mercos_Adaptor.
- `compare_products`: percorre candidatos, até 500, para comparação pelo modelo.
  Exceder o limite ou falhar uma página exige refinar; não anuncia seleção parcial
  como catálogo completo. Leituras não são um snapshot transacional entre páginas.
- `get_product` e `check_inventory`: preservam validade dos fatos, sem transformar
  preço/estoque ausente ou vencido em zero.
- `search_knowledge`: documentos aprovados e vigentes de `metadata.knowledge_documents`.
- `request_human`: somente solicitação explícita detectada pelo backend; sinaliza
  o contrato existente de handoff, sem prometer atribuição concluída.

O texto completo da persona ativa orienta identidade, personalidade e atendimento.
Ferramentas do modelo não oferecem cadastro, pedido, pagamento nem consulta
privada de cliente. O cadastro reutiliza o handler determinístico do legado antes
do modelo: valida disponibilidade, coleta os campos, apresenta revisão mascarada,
exige confirmação e consulta duplicidade antes da criação. O provider mantém a
trava durável por documento e não repete um POST de resultado desconhecido.
O preview administrativo recusa cadastro e nunca consulta ou cria clientes reais.
Compras são orientadas ao catálogo oficial.

Estado operacional é recuperado pelo mesmo mecanismo do legado e isolado por
workspace e identidade. Revisões só autorizam criação depois da entrega; um
"sim" após mudança de assunto pede nova revisão. Turnos identificados como
cadastro são excluídos do histórico enviado ao modelo, e o rascunho não entra
nas instruções. As ferramentas de consulta continuam separadas das mutações.

Conversations API mantém contexto remoto. O ID é salvo nos metadados existentes.
Somente turnos entregues com mesmo tenant, workspace, provedor, canal, conversa e cliente
autorizam reutilização. A cauda remota é comparada à última resposta entregue;
divergência, troca de persona/conhecimento ou 30 turnos reconstrói a conversa com
até 30 pares entregues. Não há continuidade automática entre IDs de conversa.
Referências de produtos são preservadas sem preço/estoque antigo.

Este caminho armazena conversas na OpenAI, independentemente do `store=False` do
legado. Conversas antigas não são apagadas automaticamente. O histórico local
continua sendo a autoridade de entrega; não existe memória infinita.

Imagens de hosts permitidos usam o downloader protegido existente e são enviadas
ao mesmo modelo. Áudio usa transcrição assíncrona com download limitado a 12 MB,
validação de host a cada redirecionamento e sem retries. A lista de hosts é a
configuração existente `INSTAGRAM_STORY_ALLOWED_HOSTS`. Vídeo de Story pede uma foto
nesta versão; envio de fotos, publicação de vector stores e memória duradoura
adicional não estão implementados neste caminho. Documentos são consultados
localmente, com sua validade verificada a cada turno.

Contrato de ferramentas conforme a [documentação oficial de function calling](https://developers.openai.com/api/docs/guides/function-calling).

## Validação local — 2026-10-03

- Regressão completa: 4.501 testes aprovados, 23 ignorados, nenhuma falha.
- 22 testes específicos do agente direto, incluindo contrato HTTP do SDK real
  com MockTransport, sessões, escopo, persona, ferramentas, limites e falhas.
- Scanner de segredos, compilação Python, `git diff --check` e dry-run do pacote aprovados.
- Nenhuma chamada real à OpenAI, Mercos ou canais de clientes nesta validação.
- Código não publicado nem motor de produção alterado; preview real e homologação
  continuam necessários antes da ativação.
