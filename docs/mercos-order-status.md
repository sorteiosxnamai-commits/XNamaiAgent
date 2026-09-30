# Consulta de pedidos e itens

A consulta usa a listagem incremental de Pedidos V2 pelo MercosAdaptor. A conta
de produção recusa `GET /v1/orders/{id}`. `id` interno e `numero` comercial são
distintos; colisões entre os dois falham sem escolher um pedido arbitrariamente.

## Atualização e histórico

Aplicar `sql/029_mercos_order_status_index.sql` e depois
`sql/20260930055006_mercos_order_sync_background.sql` e
`sql/20260930063843_mercos_order_contents.sql` e
`sql/20260930070744_mercos_order_contents_history.sql`. As tabelas têm RLS e não
concedem acesso a `anon`/`authenticated`. Armazenam somente identificadores,
status, faturamento, exclusão, instantes de verificação e os itens e totais
do pedido. A projeção exclui documentos, contatos, endereços e observações.

`get_order_complete` devolve `items_confirmed=true` somente quando a listagem
Mercos trouxe todos os itens válidos. Itens excluídos não entram na contagem.
`contents=NULL` é dado ainda não confirmado, nunca pedido vazio. Os preços
são os registrados no pedido, sem substituir pelos preços atuais do catálogo.
Perguntas sobre itens e suas continuações precedem a seleção de produtos.
Perguntas como "Okay e o que faço agora?" usam o pedido ativo e consultam seu
status antes de orientar sobre os próximos passos. Não geram uma busca por
"okay" no catálogo, e faturamento não é tratado como confirmação de pagamento.
Respostas com contagem de itens sem evidência são bloqueadas pelo validador.
Pedidos longos continuam com "continue", preservando a posição e sem cortar linhas.
As migrações retomam também o histórico antigo para preencher itens dos pedidos
indexados antes dessa mudança; essa carga continua gradualmente pelo agendador.

`POST /api/admin/commerce/sync/orders` (ADMIN_API_TOKEN) e
`POST /api/cron/commerce/sync/orders` (MERCOS_ORDER_SYNC_SECRET) executam o mesmo serviço:

1. Até duas páginas de alterações recentes, com cursor persistido.
2. Após alcançar as alterações mais recentes, uma página do histórico completo,
   iniciada sem filtro de data e retomada por um cursor independente.
3. Cada chamada de background à Mercos tem limite de 75 segundos, para acomodar
   a fila e o backoff do adaptador (até 225 segundos de HTTP por execução, dentro
   dos 300 segundos configurados no projeto Vercel). O lote possui lock por tenant
   e confirma registros e cursor juntos. Se falhar, o lote é revertido.

O histórico não substitui status, cancelamentos ou exclusões já conhecidos pelo
fluxo incremental; somente preenche itens ainda ausentes. Sua conclusão não altera a validade do
incremental. Depois de concluído, deixa de fazer chamadas de backfill.

Um pedido pode ser consultado quando o incremental completo foi confirmado nos
últimos cinco minutos **ou** quando aquele registro foi verificado nesse período.
Assim, páginas pendentes de outros pedidos não bloqueiam um resultado atual.
Dados vencidos continuam bloqueados quando a atualização falha.

O atendimento tenta no máximo duas páginas, com 12 segundos por página, se faltar um resultado atual. Um
número ausente continua sendo **não confirmado**, nunca prova de inexistência.
Durante a carga histórica, pedidos antigos podem ainda não estar disponíveis.

## Agendamento

O agendador de produção é Supabase Cron (`pg_cron` + `pg_net`), a cada dois minutos.
Ele chama somente o endpoint autenticado da aplicação; a aplicação faz apenas
GET no MercosAdaptor. O job `xnamai-mercos-order-sync` é independente dos demais.

Após publicar o endpoint e aplicar a migração, configurar com DATABASE_URL e
MERCOS_ORDER_SYNC_SECRET no ambiente, sem registrar seus valores:

```powershell
python scripts/configure_order_sync.py --base-url https://x-namai-agent.vercel.app
```

O comando cria/atualiza o mesmo job e guarda a credencial no Supabase Vault.
O SQL do agendamento contém apenas o nome do segredo. Reexecutar não duplica jobs.
Para desativar: `select cron.unschedule('xnamai-mercos-order-sync');`.

Verificar o evento `commerce.sync.orders` nos logs da aplicação e as respostas
HTTP em `net._http_response`: o cron confirma o despacho HTTP, não o sucesso do
sync. `incremental.complete` e `history.complete` têm significados independentes.
Uma falha de histórico não desfaz a atualização recente já confirmada.

`GET /api/admin/commerce/orders/{numero}` testa a consulta sem enviar mensagem ao
cliente. A resposta não transforma faturamento em pagamento, não inventa rastreio
e não expõe dados pessoais. A liberação do fluxo consultivo permanece em 5%.

## Listagem para atacado e persona

As categorias pedidas juntas são consultadas separadamente. A busca obrigatória
aplica todos os termos e normaliza aliases e potência (`120 W` = `120W`, não
`1200W`). Nunca remove um filtro para preencher a lista com outra categoria.
O offset fica no estado da conversa; "continue" percorre as páginas restantes,
sem limite total de 3, 10 ou 100 produtos. O tamanho de cada resposta é ajustado
ao canal, e os produtos exibidos permanecem selecionáveis por posição.
Preços/estoques vencidos são omitidos. Erro de consulta não vira ausência de produto.

Saudações iniciais, repetidas e de retomada usam a identidade publicada.
As respostas determinísticas da XNamai também mantêm o estilo leve com emojis.

Referências: [Pedidos Mercos V2](https://docs.mercos.com/reference/v2pedidos),
[agendamento com Cron e Vault](https://supabase.com/docs/guides/functions/schedule-functions).
