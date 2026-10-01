# Continuidade e qualidade da Mai

A interpretação da OpenAI identifica objetivo, referências e mudanças de filtros. Uma mesma rota consultiva responde perguntas e explicações sem consultar o catálogo quando não há necessidade de dados atuais. Compras, cadastro, pagamento e consulta de pedidos continuam passando pelas ferramentas e pelas confirmações da aplicação.

## Contexto e memória

- `conversation_lifecycle.py` reconcilia assunto, produtos apresentados e pergunta pendente depois da validação, crítica e composição final.
- Para mensagens de canais reais, o contexto do diálogo e o resumo são persistidos após a confirmação de envio registrada em `insert_agent_response`. Uma falha de envio não transforma uma revisão não entregue em autorização.
- Fatos de operações concluídas ou de resultado incerto são preservados antes do envio, evitando repetir operações em um retry.
- `conversation_memory.py` sanitiza objetivos, preferências, correções e perguntas pendentes de todas as rotas. Não aprende regras comerciais a partir da resposta do modelo. Resumos não autorizam operações e não substituem dados atuais do provedor.
- O resumo usa o workspace e a identidade da conversa. Os controles existentes `AGENT_CONVERSATION_SUMMARY_ENABLED`, `AGENT_CONVERSATION_SUMMARY_MODE` e `AGENT_CONVERSATION_SUMMARY_IN_PROMPT_ENABLED` precisam estar habilitados para gravação e utilização (`enforce` para gravar).
- Filtros do catálogo são estruturados. Um refinamento preserva as condições anteriores e substitui somente as condições explicitamente alteradas. Uma mudança de categoria descarta os filtros da categoria anterior.

## Validação e recuperação

Explicações gerais sobre giro, reposição e pedidos não são afirmações sobre um pedido específico. A validação distingue esses casos. A política de acréscimo aceita paráfrases com percentual, público e sentido corretos, desde que exista a regra publicada.

Quando uma resposta informativa contém afirmações sem evidência, há no máximo uma tentativa de reformulação, sem ferramentas. O texto só é aceito se passar novamente pelo validador. Operações e resultados incertos não são repetidos por esse mecanismo.

A revisão auxiliar em modo `shadow` tem prazo total de oito segundos, incluindo tentativas do provedor. Ao excedê-lo, é cancelada e registrada como não avaliada; a resposta já validada segue. Esse limite não se aplica ao modo `enforce` nem desativa a validação factual obrigatória.

## Verificação antes de publicar

O CI continua executando testes de regressão e avaliações offline. Testes com mocks não garantem o comportamento do modelo real. Antes de promover uma versão, execute também as jornadas com OpenAI e integrações configuradas em um ambiente de teste:

```powershell
python scripts/evaluate_conversation_journeys.py --base-url https://SEU-AMBIENTE --output .vercel/journey-report.json
```

Configure `ADMIN_API_TOKEN` pelo ambiente, sem colocá-lo no comando. O endpoint `/api/test/agent` é autenticado. Um preview protegido precisa também permitir acesso pelo mecanismo de proteção da Vercel. O script usa identidades sintéticas, preserva o histórico de cada jornada, não envia WhatsApp e não solicita criação de pedidos ou pagamentos. Ele pode persistir o contexto dessas identidades no banco configurado.

As jornadas em `tests/evals/fixtures/mai_journeys.json` cobrem catálogo → CPF, orientação comercial, número de pedido → próximos passos, refinamento de potência/Bluetooth e política do Club. Revise as respostas completas, além das verificações automáticas. Os números de pedidos e a disponibilidade do catálogo dependem do ambiente; ajuste as referências para dados de teste autorizados quando necessário.

Compare falhas por jornada, consulta desnecessária ao provedor, perda de filtros, handoffs e latência. Não use apenas a contagem de testes ou de respostas HTTP 200 como medida de qualidade. O evento `conversation.finalized` registra a rota e indicadores do contexto final sem registrar dados pessoais.
