# Consulta de status de pedidos

A consulta usa a listagem incremental de Pedidos V2 pelo MercosAdaptor. A conta
de produção recusa `GET /v1/orders/{id}`. `id` interno e `numero` comercial são
distintos; colisões entre os dois falham sem escolher um pedido arbitrariamente.

## Atualização e histórico

Aplicar `sql/029_mercos_order_status_index.sql` e depois
`sql/20260930055006_mercos_order_sync_background.sql`. As tabelas têm RLS e não
concedem acesso a `anon`/`authenticated`. Armazenam somente identificadores,
status, faturamento, exclusão e instantes de verificação, sem dados pessoais.

`POST /api/admin/commerce/sync/orders` (ADMIN_API_TOKEN) e
`POST /api/cron/commerce/sync/orders` (CRON_SECRET) executam o mesmo serviço:

1. Até duas páginas de alterações recentes, com cursor persistido.
2. Após alcançar as alterações mais recentes, uma página do histórico completo,
   iniciada sem filtro de data e retomada por um cursor independente.
3. Cada chamada de background à Mercos tem limite de 75 segundos, para acomodar
   a fila e o backoff do adaptador (até 225 segundos de HTTP por execução, dentro
   dos 300 segundos configurados no projeto Vercel). O lote possui lock por tenant
   e confirma registros e cursor juntos. Se falhar, o lote é revertido.

O histórico não substitui registros já conhecidos pelo fluxo incremental,
inclusive cancelamentos e exclusões. Sua conclusão não altera a validade do
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
CRON_SECRET no ambiente, sem registrar seus valores:

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

Referências: [Pedidos Mercos V2](https://docs.mercos.com/reference/v2pedidos),
[agendamento com Cron e Vault](https://supabase.com/docs/guides/functions/schedule-functions).
