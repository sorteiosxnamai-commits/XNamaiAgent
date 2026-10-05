# Cadastro interrompido após confirmação

Conversa de Ricardo analisada no ambiente XNamai em 05/10/2026, mensagens
1265, 1268–1271. Dados pessoais não foram copiados para este documento ou testes.

A Mai ofereceu cadastro por CPF, coletou documento, nome e e-mail, mostrou a
revisão e, após "sim", informou indisponibilidade. O "sim" era uma confirmação
válida para o handler legado. A criação estava habilitada, mas as tabelas
`ai_mercos_customer_document_index`, `ai_mercos_customer_index_config` e
`ai_mercos_customer_creation_claim` não existiam. Assim, a capacidade de verificar
duplicidade/criar cliente nunca estava disponível. Os chamadores permitiam
coleta mesmo sem essa capacidade e só bloqueavam na confirmação.

## Correção

- Legado verifica as duas capacidades antes de solicitar dados, inclusive nas
  respostas curtas "CPF" e perguntas sobre atendimento por CPF.
- Motor direto reutiliza validação, coleta, revisão, confirmação, consulta de
  duplicidade, criação e tratamento de resultado incerto do handler existente.
- Mutações não são ferramentas livres do modelo. O preview não cadastra clientes.
- Entrega da revisão e estado operacional seguem o mecanismo existente; uma
  resposta incerta não libera repetição e uma revisão não entregue não autoriza.
- Histórico de cadastro não é enviado à Conversations API na reconstrução.

## Ambiente publicado

A migração `restore_customer_registration_index` foi aplicada ao banco que
atende a XNamai, restaurando os contratos de `sql/025` e `sql/027`. As três
tabelas têm RLS e nenhum acesso para PUBLIC, anon ou authenticated; o backend
mantém acesso. A consulta de privilégios confirmou essa configuração. O advisor
reporta apenas o informativo esperado de
[RLS sem política para clientes](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy)
nessas tabelas privadas de operação.

Após a migração, `/api/health` mudou de `index_unavailable` para `never_synced`.
Ainda não há baseline de clientes. A
[execução da sincronização](https://github.com/sorteiosxnamai-commits/XNamaiAgent/actions/runs/37357718433)
falhou antes de chamar a aplicação: os secrets `AGENT_BASE_URL` e `CRON_SECRET`
estavam vazios. Configurar a URL do agente e o mesmo segredo de cron já utilizado
na aplicação, executar **Customer Index Sync** e verificar
`mercos_customer_index_ready=true`. Não marcar baseline manualmente nem liberar
criação sem sincronizar os clientes existentes.

Código do fluxo corrigido ainda não publicado. A ativação do motor direto não
foi alterada. Nenhum cadastro real ou mensagem ao cliente foi enviado nesta correção.

## Validação

Suíte offline completa: **4.514 aprovados, 23 ignorados**, nenhuma falha.
Inclui reprodução com dados sintéticos da sequência CPF → nome → e-mail → revisão
→ "sim", cliente existente, resultado incerto, falta de capacidade, preview,
desvio de assunto e revisão não entregue.
