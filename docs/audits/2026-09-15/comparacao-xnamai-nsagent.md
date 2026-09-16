# Comparação XNamaiAgent × NSAgent

Data: 15/09/2026.

Este é o diagnóstico anterior às alterações. A implementação de 16/09 e seus limites estão no [guia de confiabilidade da Xnamai](../../xnamai-reliability.md).

## Resultado

A Xnamai apresenta atraso relevante nas camadas compartilhadas de operação: recuperação de entregas, configuração por workspace, conhecimento documental, aprendizado, diagnóstico e algumas proteções HTTP. Ao mesmo tempo, possui uma implementação comercial própria que deve ser preservada: catálogo genérico, integração Mercos, continuidade por identidade de produto, carrinho local e revisão de pedido.

O trabalho indicado é portar melhorias selecionadas da infraestrutura do NsAgent, adaptando-as à Xnamai. Substituir o projeto pelo NsAgent reintroduziria dependências e regras de NewStore/Tray/relógios/sorteios que já foram removidas ou contornadas.

## Escopo e evidência

| Projeto | Branch | Commit comparado |
| --- | --- | --- |
| XNamaiAgent | main | `3065fd691e3b4b6672f4ba234c14c0b9dfe051f6` |
| NSAgentForSorteios | main | `d090df4459af21a70e2505544ba2031848cc27bc` |

A Xnamai foi atualizada pelo pull anterior. O remoto do NsAgent foi consultado com `git fetch origin`; HEAD e origin/main estavam iguais. Arquivos não rastreados do NsAgent não foram usados como implementação de referência.

Esta análise compara código, testes e configuração versionada. Não verifica o estado dos bancos, variáveis ou deploys de produção. Documentos de auditorias anteriores ajudam a localizar alterações, mas suas medições não são tratadas como resultados desta comparação.

Foram executados **395 testes da Xnamai, todos aprovados**, nos seguintes conjuntos: identidade, catálogo genérico, resolução e execução de continuidade, revisão de pedidos, condições de pagamento, isolamento comercial e adaptador/webhook YCloud. As credenciais foram removidas do processo de teste, a leitura de arquivo de ambiente foi desabilitada e conexões reais ao banco foram bloqueadas. A suíte completa e os testes do NsAgent não foram executados nesta comparação.

## 1. Entrega e recuperação de mensagens — prioridade imediata

**Xnamai:** em falha de envio, o worker grava a resposta na outbox e marca a entrada como falha. Existem funções para reivindicar e finalizar saídas, mas a busca em `app/` e `api/` não encontrou consumidor chamando essas funções. O agendamento versionado de `process-inbox` é diário; a rota YCloud também tenta processar entradas imediatamente.

**NsAgent:** possui consumidor de outbox, despacho de ambas as filas, recuperação de leases expirados, proteção do dono do lease, deduplicação e descarte de respostas superadas por mensagens posteriores.

**Impacto:** a resposta salva para reenvio na Xnamai pode ficar pendente; reprocessar uma entrada não equivale a reenviar a resposta já produzida. Essa lacuna afeta a confiabilidade do atendimento independentemente do provedor comercial.

**Ação:** adaptar consumidor, envelope de reenvio, controle de leases e despacho à rota YCloud. Preservar destinatário, provedor, conteúdo e metadados da resposta aceita.

Evidência: [worker Xnamai](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/ingress/worker.py:118>), [outbox Xnamai](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/ingress/outbox.py:69>), [consumidor NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/ingress/outbox_worker.py:37>), [despacho NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/ingress/dispatch.py:34>).

## 2. Configuração comercial pelo workspace — prioridade alta

**Xnamai:** já possui persona versionada, memória e extensões de instruções. Entretanto, o runtime ainda depende de Settings, constantes e textos no código. Não possui o equivalente a `app/configuration/` e ao carregamento de políticas publicadas por workspace do NsAgent.

**NsAgent:** carrega configurações publicadas, resolve o workspace da conversa e aplica políticas e mensagens ao turno. Seu seed atual contém **464 definições, incluindo 255 chaves de mensagens**; isso é a contagem do arquivo versionado, não uma conferência do banco em produção.

**Impacto:** ajustes comerciais que o NsAgent consegue consumir como configuração continuam exigindo alteração de código/ambiente na Xnamai. Persona em banco, isoladamente, não fornece o mesmo controle.

**Ação:** portar o mecanismo e cadastrar políticas próprias da Xnamai. A integração depende também de migrações e dos componentes de administração; o backend e frontend da Xnamai não foram auditados aqui.

Evidência: [compilador Xnamai](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/prompt_compiler.py:121>), [runtime de configuração NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/configuration/runtime.py:36>), [resolução de workspace](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/configuration/workspace.py:5>).

## 3. Documentos e conhecimento institucional — prioridade alta

**Xnamai:** o compilador recebe `relevant_knowledge`, mas o descarta explicitamente como reservado para fases posteriores. Não possui o repositório de anexos da persona presente no NsAgent.

**NsAgent:** seleciona trechos relevantes dos anexos, identifica fonte e hash de versão, filtra documentos expirados e monta conhecimento contextual para a pergunta.

**Impacto:** a Xnamai está atrás na capacidade de responder com evidência documental rastreável sobre políticas, condições comerciais e outros conteúdos da empresa. Isso é diferente da consulta estruturada de produtos Mercos, que já existe.

Evidência: [parâmetro ignorado na Xnamai](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/prompt_compiler.py:111>), [recuperação documental NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/persona/persona_knowledge_repository.py:86>), [validade dos anexos](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/persona/persona_knowledge_repository.py:185>).

## 4. Aprendizado com evidências válidas — prioridade alta

**Xnamai:** a consulta de atendimentos filtra por data e limite, sem exigir entrega bem-sucedida. O próprio código informa que o tenant ainda não é aplicado à consulta.

**NsAgent:** seleciona a entrega válida mais recente por entrada, exclui respostas vazias, `skipped` e `dry_run`, evita reaprender duplicatas e possui tratamento de aprendizado por workspace.

**Impacto:** quando o aprendizado está habilitado, a Xnamai pode usar como evidência respostas que não chegaram ao cliente. O isolamento também precisa evoluir antes de ampliar o uso com múltiplos workspaces. Não foi verificado se esse recurso está ativo em produção.

Evidência: [coleta Xnamai](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/attendance_learning.py:45>), [coleta NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/learning/cursor.py:65>), [agrupamento por workspace](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/learning/attendance_learning.py:549>).

## 5. Recuperação de mal-entendidos — evolução parcial

**Xnamai:** já resolve seleção por posição, continuidade de produto, preço, estoque, troca de item e carrinho. Os testes selecionados confirmam essas capacidades.

**NsAgent:** acrescenta uma camada explícita para reclamações como “não foi isso que perguntei”: recupera preferências, refaz uma busca de leitura e encaminha ao humano após o limite configurado de tentativas. Possui também correções para contexto antigo de checkout e critérios preservados após fallback.

**Ação:** adaptar essa recuperação ao resolvedor genérico da Xnamai, preservando identidade de produto e evitando importar o classificador especializado em relógios.

Evidência: [continuidade Xnamai](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/commerce/turn_flow.py:1>), [recuperação NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/sales/conversation_repair.py:39>), [regressões NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/tests/sales/test_conversation_recovery_regressions.py:66>).

## 6. Mídia no WhatsApp — lacuna do transporte YCloud

**Xnamai/YCloud:** o parser aceita somente `text`; áudios, imagens e outros tipos são ignorados por essa implementação. A saída também publica somente texto. O fluxo comercial conseguir localizar uma foto não significa que o transporte a envie como imagem.

**NsAgent/Brevo:** possui montagem e envio de imagens como anexos, incluindo múltiplas imagens.

**Ação:** implementar entrada de áudio/imagem e saída de mídia no adaptador YCloud, conforme as necessidades do atendimento. O código de transporte Brevo serve como referência de comportamento, mas exige adaptação.

Evidência: [tipos YCloud](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/channels/ycloud_whatsapp.py:32>), [saída YCloud](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/channels/ycloud_whatsapp.py:297>), [imagens NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/channels/brevo_client.py:90>).

## 7. Desempenho, diagnóstico e HTTP — prioridade de infraestrutura

- **Banco:** Xnamai abre e fecha uma conexão por `get_conn()`. NsAgent tem pool limitado e cache de leituras por turno. A redução de latência precisa ser medida no ambiente Xnamai.
- **Diagnóstico:** a Xnamai já tem métricas por turno, mas o worker inspecionado não passa `response_metadata` ao registro da resposta. NsAgent persiste metadados e associação ao workspace para investigação das execuções.
- **HTTP:** NsAgent limita corpos a 1 MiB inclusive em streaming; as rotas inspecionadas da Xnamai leem `request.body()` sem esse limite de aplicação.
- **CI:** NsAgent exige cobertura mínima de 70% com branches e executa avaliações offline em job próprio. A CI da Xnamai não tem essas duas exigências.
- **Dependências:** o NsAgent fixa versões diferentes de FastAPI e python-dotenv e inclui Starlette, python-multipart e psycopg-pool explicitamente. Não foi feita auditoria atual de vulnerabilidades nesta comparação.

Evidência: [conexão Xnamai](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/db.py:28>), [pool NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/core/connection_pool.py:13>), [limite HTTP NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/app/http/payload.py:17>), [CI NsAgent](<D:/Documents_Old/NewStore/deploy-prod/NSAgentForSorteios/.github/workflows/ci.yml>).

## Capacidades próprias da Xnamai a preservar

1. Fronteira comercial independente do fornecedor e capacidades expostas somente quando suportadas.
2. Índice Mercos local com sincronização, paginação e validade de preço/estoque.
3. Identificação genérica de produto: marca semelhante não basta para afirmar que encontrou o item.
4. Continuidade por ID de produto e estado comercial entre mensagens.
5. Carrinho local e revisão com preços/estoque consultados, condições de pagamento e fingerprint.
6. Identidade Xnamai nos prompts principais e isolamento entre tenant comercial e lookup legado de persona.

**Limitação funcional atual:** o provider ainda marca `create_order` como `UNSUPPORTED`. A revisão prepara o pedido, mas esta versão não fecha o ciclo de criação no Mercos. Esse é um próximo passo próprio da Xnamai; copiar checkout Tray, PIX ou sorteios não resolve a integração Mercos.

Evidência: [matcher genérico](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/commerce/generic_catalog.py:1>), [filtros Mercos](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/commerce/mercos/catalog_search.py:8>), [revisão](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/commerce/order_review.py:303>), [criação desabilitada](<D:/Documents_Old/xNamai/Chatbô/XNamaiAgent/app/commerce/mercos/provider.py:134>).

## Resíduos de identidade e documentação

Ainda existem defaults `newstore`/`newstore_commercial` no lookup de persona, textos herdados em partes da busca especializada e README identificado como NewStoreAgent. Os testes de identidade ativa passaram; portanto, a presença desses resíduos não prova que toda resposta atual exponha a marca antiga. O tenant comercial já está separado como Xnamai.

Também há documentação Mercos que ainda descreve o schema como desconhecido, embora o código posterior já normalize preço/estoque e tenha testes correspondentes. A documentação precisa acompanhar a implementação.

Alterar as chaves de persona exige conferir o cadastro existente e preparar sua migração, para não perder o carregamento da persona ativa.

## Ordem recomendada

1. Recuperação de entrega/outbox e proteções HTTP; validar envio e retentativa YCloud.
2. Configuração por workspace, rastros de execução e conhecimento documental, com as migrações necessárias.
3. Integridade do aprendizado e recuperação de mal-entendidos sobre o catálogo genérico.
4. Áudio/imagem no YCloud e conclusão do fluxo de pedidos Mercos, conforme a prioridade comercial.
5. Pool/cache, avaliações de conversas Xnamai na CI e atualização da documentação/identificadores legados.

Não há base nesta análise para expressar o atraso em percentual, número de dias ou ganho de conversão. Ambos os projetos receberam commits em 15/09; a diferença relevante é quais capacidades e garantias chegaram a cada implementação.
