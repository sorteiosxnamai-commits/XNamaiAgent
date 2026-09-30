# Consulta de status de pedidos

A conta de produção recusou `GET /v1/orders/{id}` com 403. A consulta usa a
listagem incremental do adaptador, que encaminha para Pedidos V2. O contrato
oficial distingue `id` de `numero`: https://docs.mercos.com/reference/v2pedidos.

Aplicar `sql/029_mercos_order_status_index.sql`. As tabelas têm RLS e não concedem
acesso a anon/authenticated. Armazenam somente identificadores, status,
faturamento e marcador de exclusão; descartam documentos, contatos, itens e totais.

`POST /api/admin/commerce/sync/orders`, com ADMIN_API_TOKEN, sincroniza até três
páginas por chamada. Repetir até `complete=true`. Cada página tem até 20 pedidos
no adaptador. Cursor e registros são confirmados juntos em transação; um lock
por tenant impede dois escritores. Falha reverte o lote sem avançar o cursor.

A carga inicial cobre alterações dos últimos **sete dias**, não todo o histórico.
Pedidos carregados permanecem no índice, e as alterações seguintes são incrementais.
Um número ausente não permite concluir que o pedido não existe. Consultas com
ID/número ambíguo ou status desconhecido também retornam informação não confirmada.
Pedidos antigos sem alteração nessa janela exigem uma carga histórica adicional.

O snapshot vence após cinco minutos. A consulta tenta atualizar até duas páginas,
com 12 segundos por página; sincronização incompleta ou falha não libera dados
antigos. Não há varredura ilimitada durante a conversa. Após período de muita
atividade, concluir o sync administrativo antes de validar o atendimento.

`GET /api/admin/commerce/orders/{numero}` testa o caminho real sem enviar mensagem
ao cliente. `get_order` e `get_order_complete` oferecem apenas status e faturamento,
sem promover orçamento a pedido, faturamento a pagamento ou inventar rastreamento.
As ferramentas continuam fora da superfície livre do modelo; o fluxo de pedido
identificado chama a consulta determinística.
