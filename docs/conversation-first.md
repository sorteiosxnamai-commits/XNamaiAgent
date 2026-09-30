# Conversa consultiva da Mai

`AGENT_CONVERSATION_FIRST_ENABLED=true`, com `AGENT_CONSULTATIVE_ENABLED=true`
e `AGENT_TURN_UNDERSTANDING_ENABLED=true`, habilita interpretação contextual antes
dos atalhos comerciais. `AGENT_CONSULTATIVE_EMERGENCY_OFF=true` desliga também
essa entrada. A porcentagem do consultivo antigo continua controlando apenas
suas consultas de produtos; a entrada nova cobre orientação informativa.

O interpretador separa `conversation_mode=advice` de `operational` e registra
todas as perguntas em `questions`. O mesmo resultado é reaproveitado nos caminhos
seguintes. Respostas curtas são interpretadas com histórico, tópico e pergunta
pendente. Imagens, ações e dados de cadastro ou transação mantêm seus
responsáveis específicos. Dúvidas informativas durante um cadastro podem ser
respondidas sem cancelar ou confirmar o cadastro.

No modo informativo o modelo pode usar conhecimento geral sem ferramentas. A
única ferramenta disponível é `search_knowledge`, para conteúdo institucional
aprovado. Ele não recebe ferramentas de carrinho, pedido, pagamento ou cadastro.
Ter um carrinho/pedido no contexto não impede uma explicação. O histórico não
prova preço, estoque ou status atual.

Listagens atacadistas preservam a busca com filtros e continuação sem limite
global de itens. Perguntas institucionais junto da listagem recebem orientação
adicional sem reescrever os produtos verificados. Próximos passos de um pedido
usam consulta confirmada antes da composição. Validação factual continua ativa;
quando há substituição, `factual_validation_initial` mantém a causa original.

## Avaliação

`scripts/evaluate_ricardo_conversations.py` contém casos anonimizados de orientação,
confirmação de oferta de catálogo, cadastro, múltiplas perguntas, comparação
geral, correção, checkout, catálogo e pedidos. Usa `XNAMAI_EVAL_URL` e
`ADMIN_API_TOKEN` no ambiente; não necessita da chave OpenAI local.

O endpoint administrativo `/api/test/agent` aceita `history` opcional com até
24 mensagens, somente papéis user/assistant e até 4000 caracteres por mensagem.
Não grava essas mensagens como entregues nem envia mensagens ao cliente. Use
identidades sintéticas, pois o fluxo pode atualizar sua memória de atendimento.

As verificações automáticas cobrem roteamento e evidência. A qualidade das
explicações também requer leitura das respostas reais; respostas de mocks não
são prova de qualidade do modelo. Para rollback, desligue a flag nova e publique
novamente, preservando os caminhos comerciais anteriores.
