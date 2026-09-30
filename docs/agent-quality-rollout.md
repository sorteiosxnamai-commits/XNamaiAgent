# Ativação das melhorias do agente XNamai

## Destino

Confirme no painel da Vercel o projeto **XNAMAI agente**, a equipe proprietária e
o repositório `XNamaiAgent`. O vínculo local encontrado em
`.vercel/project.json` aponta para outro projeto e **não é autorização
para publicar nesse destino**. Relacione o projeto correto antes de usar deploy.

## Preparação da base

`docs/knowledge/xnamai.review.json` contém apenas candidatos migrados das regras
antigas do repositório. Todos estão em `draft`: os valores não foram confirmados
em produção. Revise conteúdo, fonte e validade, mude para `approved` somente os
documentos confirmados e preserve os documentos já existentes da persona ativa.
Não envie conversas de clientes ao índice. Uma política por tópico; ambiguidades
devem ser resolvidas antes da publicação.

As rotas abaixo usam `Authorization: Bearer <ADMIN_API_TOKEN>`. Execute contra o
projeto correto; nunca copie a chave OpenAI para o computador. O servidor usa sua
própria `OPENAI_API_KEY`. `workspace_id` é obrigatório e deve ser o UUID real.

1. `GET /api/admin/agents/xnamai/readiness?workspace_id=UUID` verifica configuração
   efetiva, modelo, persona e cobertura do índice, sem retornar segredos. Não faz
   uma chamada ao modelo nem prova disponibilidade da OpenAI ou schema de memória.
2. Obtenha a persona e seu ID com as rotas administrativas existentes. Confira o
   workspace antes de prosseguir. `POST /api/admin/agents/xnamai/personas/ID/knowledge-draft?workspace_id=UUID`
   recebe `{"knowledge_documents": [...]}` e cria uma cópia em rascunho, preservando
   instruções e demais metadados. A lista recebida substitui a lista inteira no
   rascunho; inclua também os documentos anteriores que devem continuar publicados.
3. `POST /api/admin/agents/xnamai/personas/NOVO_ID/knowledge-index?workspace_id=UUID`
   cria o store na primeira chamada; nas próximas, envia no máximo um documento.
   Repita manualmente ou com intervalo até `status=ready`. Não há espera bloqueante
   por processamento dentro da função. Há limite de 100 documentos de 100 mil
   caracteres e timeout de cinco segundos por chamada OpenAI, sem retries.
   Uma transação bloqueia a linha do rascunho durante a etapa para evitar uploads
   simultâneos. Falha retorna erro; não ativa nem altera a versão publicada.
4. Confira `completed == total`, conteúdo e manifesto. Ative o novo ID pela rota
   `/activate` já existente. Mantenha o ID da versão anterior para rollback.

Stores são mantidos entre etapas e versões. Uploads que falham no vínculo são
removidos; uma queda entre criação remota e commit do banco pode deixar recursos
órfãos. Confira o armazenamento OpenAI após falhas; não remova stores usados por
versões ativas ou necessárias para rollback.

## Validação e ativação gradual

- Confirme no projeto correto uma duração de função de pelo menos 60 segundos
  para as rotas administrativas. A configuração efetiva da Vercel não foi
  verificada; o arquivo legado de build não define essa duração.
- Confirme o modelo efetivo (`OPENAI_MAIN_MODEL` tem precedência sobre
  `OPENAI_MODEL`) e a migração `sql/010_ai_memory_proposals.sql` antes de habilitar
  memória. Variáveis existentes na Vercel prevalecem sobre os novos defaults.
  Configurações ativas em `workspace_agents.configuration.runtime.values`
  prevalecem também sobre as variáveis de ambiente; confirme o modelo efetivo
  no endpoint de readiness e altere o workspace quando houver override.
- Habilite `AGENT_KNOWLEDGE_SEARCH_ENABLED=true` somente após publicar o índice.
- Comece com `AGENT_CONSULTATIVE_ENABLED=true`,
  `AGENT_CONSULTATIVE_TRAFFIC_PERCENT=5` e
  `AGENT_CONSULTATIVE_EMERGENCY_OFF=false`. O percentual é **0 a 100** e a seleção
  permanece estável por workspace, canal e conversa. O modo exige interpretação
  estruturada, persona ativa e identidade da conversa.
- O modo faz até três rodadas OpenAI, seis chamadas de ferramenta e duas buscas
  de catálogo. Cada busca revalida até três produtos. Só expõe conhecimento e
  busca de produtos; nenhuma ferramenta de dados de clientes ou mutação.
- Confirmações, carrinhos, cadastro, pedidos e acompanhamentos pendentes continuam
  no fluxo transacional existente. Todos os resultados passam pela validação
  factual normal do pipeline. Valores institucionais só têm exceção monetária
  quando reproduzem a política aprovada inteira em uma linha própria.
- Amplie para 25%, 50%, 100% apenas após revisão das respostas, falhas, latência e
  consumo. Não há promoção automática baseada nos testes offline.

## Comparação de respostas

`POST /api/admin/agents/xnamai/quality-evaluation?workspace_id=UUID` recebe uma
pergunta anonimizada com `anonymized=true`, `required` e `forbidden` opcionais.
Compara os modelos efetivos dos papéis `fast` e `main`, com até duas gerações de
15 segundos e sem ferramentas comerciais ou envio a clientes. Os critérios
esperados não são enviados ao modelo. Mede **composição de respostas institucionais**,
não a jornada transacional completa. Os papéis podem resolver para o mesmo modelo;
confira os nomes retornados antes de interpretar uma comparação.

Para verificar a integração estruturada, use `mode="structured_contracts"` no
mesmo endpoint. São duas chamadas sintéticas com os contratos reais de memória
e revisão, sem executar propostas, ferramentas ou enviar mensagens a clientes.
Este teste complementa a comparação textual: uma geração de texto bem-sucedida
não demonstra que os schemas estruturados são aceitos pela API.

Com `AGENT_ADMIN_URL` e `ADMIN_API_TOKEN` no ambiente do processo:

```powershell
python -m scripts.evaluate_agent_quality docs/evals/institutional.synthetic.jsonl --workspace-id UUID --limit 10 --output quality-report.json
```

O corpus incluído é sintético. Para avaliação real, forneça 50–100 perguntas
anonimizadas e critérios revisados, execute em lotes e analise as respostas.
O relatório contém tokens e latência; custo em dinheiro depende da tabela vigente
e não é estimado com preços fixos. Falhas de requisição interrompem a execução
sem retry automático. Relatórios são salvos após cada caso concluído. Aprovação
por palavras-chave é apenas um indicador; revisão humana continua obrigatória.

## Reversão

Defina `AGENT_CONSULTATIVE_EMERGENCY_OFF=true` para interromper o novo fluxo,
`AGENT_KNOWLEDGE_SEARCH_ENABLED=false` para voltar à busca lexical e, se necessário,
reative a persona anterior. Restaure o modelo anterior explicitamente nas variáveis.
Para desligar resumos, configure `AGENT_CONVERSATION_SUMMARY_ENABLED=false`,
`AGENT_CONVERSATION_SUMMARY_IN_PROMPT_ENABLED=false` e
`AGENT_CONVERSATION_SUMMARY_MODE=off`. Reimplante quando alterar variáveis Vercel.
