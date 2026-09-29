# Melhorias de confiabilidade e atendimento da Xnamai

Implementação de 16/09/2026, a partir da comparação com o XNamaiAgent. Mantém o catálogo genérico, o provider Mercos e a identidade da Xnamai.

## Entrega e recuperação

- O worker grava a resposta completa aceita na outbox **antes** de chamar o canal. Uma retomada reutiliza texto, destino, URL de áudio e metadados de mídia/contexto, sem gerar outra resposta.
- O envelope não guarda bytes de áudio nem uma cópia completa do webhook. A auditoria mantém o estado comercial e o resumo redigido da execução.
- As filas recuperam leases expirados, usam `FOR UPDATE SKIP LOCKED`, respeitam o dono da execução e aplicam espera exponencial entre falhas (30 até 300 segundos).
- A outbox para de tentar após o limite da linha, a janela `AGENT_OUTBOX_RETRY_WINDOW_SECONDS` ou uma mensagem posterior da mesma conversa. Antes do envio também verifica mensagens ainda na inbox e o atendimento humano. Meta preserva seu tratamento próprio de takeover.
- Uma execução que perdeu o lease não pode confirmar a entrega de outro worker. Se já existe auditoria de envio bem-sucedido, a retomada recupera o recibo sem reenviar.
- Os locks de conversa usam transações e desabilitam prepared statements automáticos. Cancelar uma requisição também libera um lock adquirido tardiamente pela thread de banco.
- Webhooks Brevo, Meta e YCloud rejeitam corpos acima de 1 MiB, inclusive sem `Content-Length` confiável. A assinatura usa os bytes originais.
- Um webhook não confirma enfileiramento quando o banco não devolveu um ID persistido.

`sent` significa que o transporte aceitou o envio. Não comprova leitura ou entrega ao dispositivo. Se o provedor aceitar uma mensagem e a conexão cair antes de devolver o recibo, o envio é ambíguo: por padrão não há retentativa (evita duplicata) e a linha fica `delivery_unknown` até um callback de status do YCloud reconciliá-la. Não há garantia de entrega exatamente uma vez.

### Ativação operacional

As tabelas são as existentes em `sql/022_inbound_inbox_outbox.sql`; este pacote não cria uma nova migração nem aplica SQL em produção. A persona usa o schema de `sql/009_ai_agent_persona.sql` e suas dependências. Mantenha `AUTO_CREATE_TABLES=false` em produção e aplique as migrações pelo processo habitual.

YCloud e Meta já passam pela inbox. Brevo usa a fila quando `AGENT_ASYNC_INGRESS_ENABLED=true`; o caminho síncrono continua disponível com o default `false`.

Escolha uma forma de consumir as filas continuamente:

1. Em um serviço de worker supervisionado, usando as mesmas configurações de banco, persona, catálogo e canais do agente:

   ```bash
   python scripts/process_queues.py --interval 10 --batch-size 5
   ```

   Para executar somente um ciclo:

   ```bash
   python scripts/process_queues.py --once
   ```

2. Em um agendador externo, chamar a cada minuto `POST /api/cron/process-inbox` com `Authorization: Bearer <CRON_SECRET>`. O endpoint GET também continua disponível. Ele processa inbox e outbox mesmo quando uma delas falha.

O cron de `vercel.json` permanece diário e serve apenas como verificação tardia. As retentativas da outbox rodam pelo workflow `.github/workflows/outbox-retry.yml` (a cada 5 min, item 3); veja a decisão em [outbox_runtime.md](outbox_runtime.md). Nenhum serviço foi iniciado ou implantado por esta alteração.

`DRY_RUN=true` simula o envio, mas ainda pode registrar e consumir linhas das filas. Use um banco de homologação para testar: um dry-run não deixa as mesmas mensagens disponíveis para enviar depois.

3. Somente a outbox, sem trabalho de LLM: `POST /api/cron/process-outbox` (mesma autenticação). Usa o mesmo consumidor (`process_outbox_batch`) do item 2; serve para um agendador frequente e barato quando a inbox já é processada inline.

Monitoramento: os eventos `queue.worker_cycle`, `outbox.batch_processed`, `outbox.receipt_rejected`, `outbox.send_exception` e `queue.dispatch_failed` mostram progresso e falhas. Toda linha de log de uma entrega traz `trace_id` (`outbox-<id>-a<tentativa>` no worker), `inbound_id`, `outbox_id` e `delivery_attempt` — nunca telefone ou chave de conversa. Linhas `dead` precisam de investigação; não as volte indiscriminadamente para `pending`.

### Política de entrega (retry, backoff, dead-letter, idempotência)

Modelo de execução, estados e reconciliação: [outbox_runtime.md](outbox_runtime.md).

Implementada em `app/ingress/delivery_policy.py`:

| Configuração | Default | Efeito |
| --- | --- | --- |
| `AGENT_QUEUE_RETRY_BASE_SECONDS` | 30 | atraso antes da 2ª tentativa; dobra a cada falha |
| `AGENT_QUEUE_RETRY_MAX_SECONDS` | 300 | teto do atraso |
| `AGENT_OUTBOX_RETRY_WINDOW_SECONDS` | 900 | resposta mais velha que isto vai para `dead` (`outbox_retry_window_expired`) |
| `AGENT_OUTBOX_LEASE_SECONDS` | 180 | lease de cada envio; lease vencido é recuperado por outro worker |
| `AGENT_OUTBOX_RETRY_UNKNOWN_DELIVERY` | false | ver "entrega ambígua" abaixo |

Com os defaults, cabem **6** das `max_attempts=8` do schema dentro da janela (0s, 30s, 90s, 210s, 450s, 750s); as tentativas restantes nunca ocorrem porque a janela expira antes.

**Nenhum provider usado oferece chave de idempotência** (YCloud: `externalId` é só correlação, sem deduplicação documentada; Meta Graph e Brevo: nenhuma). Por isso não há garantia de entrega exatamente-uma-vez:

- falha **definitiva** (conexão recusada, timeout de conexão, 429, 502, 503, 4xx de rejeição) → nova tentativa com backoff (ao menos uma vez); 4xx de rejeição vai direto para `dead`;
- falha **ambígua** (timeout de leitura, conexão caída no meio da resposta, 500, 504) → a mensagem pode ter sido entregue. Por padrão **não** há reenvio automático: a linha vai para `dead` com `last_error = delivery_unknown:<motivo>`. O YCloud recebe `externalId = outbox:<id>` para conciliar pelo webhook de status. `AGENT_OUTBOX_RETRY_UNKNOWN_DELIVERY=true` troca para ao-menos-uma-vez, aceitando possível duplicata.

Dentro de uma mesma tentativa o YCloud só repete o POST quando a requisição comprovadamente não saiu, e o Meta só tenta o endpoint alternativo após rejeição 4xx.

### Orçamento de LLM por turno

Além do teto de operações lógicas (`AGENT_MAX_LLM_CALLS_PER_TURN`), `AGENT_MAX_LLM_TRANSPORT_ATTEMPTS_PER_TURN` (default 8) limita as tentativas HTTP reais: chamada primária, fallback Responses→Chat e as repetições internas do SDK (`OPENAI_MAX_RETRIES`), contadas por um hook do cliente compartilhado. Esgotado o teto, nenhuma chamada nova começa; a requisição em andamento não é interrompida.

## Políticas publicadas com a persona

O agente lê `metadata.business_policies` da versão ativa da persona no início de cada turno. A leitura é reutilizada durante esse turno e não vaza entre conversas. Uma nova publicação entra em vigor no próximo turno. Publicação, arquivamento e rollback invalidam o cache local do turno que fez a alteração.

| Campo | Default | Limite |
|---|---|---|
| `catalog_browse_limit` | 10 | 1–10 produtos |
| `conversation_repair_handoff_after` | 2 | 1–5 reclamações consecutivas |
| `knowledge_max_chunks` | 4 | 1–4 trechos |
| `repair_ack` | Confirmação de correção | 1–300 caracteres |
| `repair_clarification` | Solicitação de produto/referência | 1–600 caracteres |
| `repair_handoff` | Encaminhamento à Xnamai | 1–600 caracteres |

Outras chaves são rejeitadas. Credenciais, conexão de banco e capacidades de mutação não podem ser modificadas por essas políticas. Se uma versão antiga tiver políticas inválidas, o runtime usa os defaults e registra o erro.

Use a API administrativa existente, autenticada com `ADMIN_API_TOKEN`:

1. Leia a persona ativa com `include_instructions=true`.
2. Crie uma nova versão preservando suas instruções e metadados, acrescentando os controles desejados.
3. Revise a versão e publique pelo endpoint `/activate`. O endpoint `/rollback` restaura a versão anterior selecionada.

Os caminhos são `/api/admin/agents/{tenant_id}/personas`; passe também o `persona_key` existente quando aplicável. Use os valores de `AGENT_PERSONA_TENANT_ID` e `AGENT_PERSONA_KEY` do deployment. Os defaults atuais são `xnamai` e `xnamai_commercial`. Revise as variáveis do deployment e publique a persona Xnamai ao atualizar instalações existentes. `COMMERCE_TENANT_ID` continua separado.

## Conhecimento documental

Documentos publicados em `metadata.knowledge_documents` passam a alimentar o compilador. Exemplo de estrutura de metadados — substitua o conteúdo pelo material aprovado da empresa:

```json
{
  "business_policies": {
    "catalog_browse_limit": 5,
    "conversation_repair_handoff_after": 2,
    "knowledge_max_chunks": 3
  },
  "knowledge_documents": [
    {
      "id": "politica-atendimento",
      "title": "Política de atendimento Xnamai",
      "status": "approved",
      "content": "Conteúdo institucional aprovado da Xnamai.",
      "valid_until": "2027-09-16T23:59:59-03:00"
    }
  ]
}
```

São elegíveis documentos `approved` ou `ready` (default `approved` dentro da persona publicada), com ID e texto. Documentos vencidos ou com data de validade inválida/sem fuso são descartados. Sem `valid_until`, o documento não expira automaticamente.

A busca é lexical, tolerante a acentos: considera até 100 documentos e os primeiros 40 mil caracteres de cada um, retornando até quatro trechos de 1.400 caracteres. O prompt identifica fonte, número do trecho e hash SHA-256 do conteúdo. Esses identificadores também entram na auditoria de compilação quando ela está habilitada.

Os trechos são referências e não substituem regras operacionais. Preço e estoque atuais continuam exigindo a fonte comercial. Não há ingestão automática de PDF, embeddings nem sincronização com anexos do painel nesta implementação.

## Recuperação de conversa

Reclamações como “não foi isso que perguntei”, “você não entendeu” e “não perguntei o preço” acionam uma correção explícita no fluxo comercial genérico. O agente usa a correção informada ou a última consulta de catálogo, preserva preferências e executa somente ações de leitura. A recuperação limpa a confirmação conversacional pendente. Sem contexto, pede o produto; após o limite de reclamações consecutivas, sinaliza handoff.

O handoff reutiliza o contrato existente do agente. Não implementa por si só atribuição de um atendente em um painel externo.

## Aprendizado e auditoria

A coleta considera somente respostas não vazias com envio marcado como bem-sucedido, excluindo `dry_run`, envios pulados e entradas já revisadas. Escolhe a última resposta válida por mensagem e exige o tenant gravado pelo servidor em `_agent_tenant_id`.

Registros antigos sem esse campo ficam fora do aprendizado. A alteração não atribui histórico legado a um tenant por suposição. Propostas continuam sujeitas à aprovação administrativa; não há autoativação de instruções.

## Validação e próximos limites

```bash
python -m pytest -q --ignore=tests/evals --cov=app --cov=api --cov-branch --cov-fail-under=70
python -m pytest -q tests/evals -m "not online_eval"
python scripts/scan_secrets.py
python scripts/package_release.py --dry-run
```

A CI exige 70% de cobertura com branches e executa avaliações offline em job próprio, incluindo conversas Xnamai com seleção de produto, preço, estoque e correção. Os testes bloqueiam conexões reais por psycopg e HTTPX; transportes de teste explícitos são permitidos.

Os testes locais usam banco/provider simulados. Eles não substituem validação de concorrência no PostgreSQL e entrega no canal em homologação.

Permanecem fora desta entrega: integração de configuração/anexos com o workspace do painel, pool de conexões compartilhado, mídia YCloud e criação real de pedidos Mercos. As capacidades comerciais atuais e as restrições de mutação foram preservadas.
