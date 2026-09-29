# Cadastro Mercos pelo ChatBÃ´

Contrato conferido em 2026-09-24: [clientes Mercos](https://docs.mercos.com/reference/v1clientes). O POST exige `razao_social` e `tipo` por padrÃ£o; cada conta pode exigir campos adicionais. CNPJ alfanumÃ©rico segue o [cÃ¡lculo oficial do DV da Receita Federal](https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/documentos-tecnicos/cnpj/manual-dv-cnpj.pdf).

O ChatBÃ´ coleta CPF/CNPJ, nome legal, e-mail e telefone em turnos curtos. Pessoa fÃ­sica usa `tipo=F` e o nome completo como `razao_social`. Pessoa jurÃ­dica usa `tipo=J`, pergunta nome fantasia de forma opcional e nunca usa o nome do WhatsApp como razÃ£o social. Telefone vÃ¡lido do WhatsApp preenche o rascunho e pode ser corrigido. O POST exige revisÃ£o, confirmaÃ§Ã£o explÃ­cita e lookup `NOT_FOUND`.

No boundary do MercosAdaptor, `document` vira `cnpj`; `email` vira `emails: [{"email": ...}]`; `phone` vira `telefones: [{"numero": ...}]`. Campos oficiais adicionais sÃ³ sÃ£o enviados quando coletados: `nome_fantasia`, `inscricao_estadual`, `suframa`, `cep`, `rua`, `numero`, `complemento`, `bairro`, `cidade`, `estado`, `observacao`. A observaÃ§Ã£o Ã© omitida por padrÃ£o. Se o cliente informar CEP e a integraÃ§Ã£o jÃ¡ configurada resolver o endereÃ§o, rua, bairro, cidade e estado sÃ£o preenchidos sem inventar nÃºmero ou complemento.

O parser determinÃ­stico dos erros 422 aceita apenas nomes de campos oficiais reconhecidos e mensagens de obrigatoriedade. O claim Ã© liberado apÃ³s rejeiÃ§Ã£o definitiva; o ChatBÃ´ pede o campo, apresenta nova revisÃ£o e exige nova confirmaÃ§Ã£o. Duplicidade explÃ­cita dispara sync e lookup; se nÃ£o resolver, handoff. Timeout, conexÃ£o interrompida e 5xx deixam o claim `unknown` e proÃ­bem retry.

Resposta 2xx sem ID Ã© criaÃ§Ã£o confirmada. O claim passa a `created_pending_sync`, mantÃ©m o segundo POST bloqueado e solicita customer sync. O Ã­ndice HMAC local associa o documento ao `customer_id` quando a listagem Mercos incluir o novo cliente; entÃ£o o claim vira `created`. Se o sync nÃ£o encontrar imediatamente, a conversa permanece pendente atÃ© a prÃ³xima sincronizaÃ§Ã£o. O Ã­ndice conserva isolamento por tenant, baseline completo e recente, fingerprint da chave, advisory lock e claim durÃ¡vel. Documento bruto nÃ£o Ã© gravado nele.

## Antes de ligar em produÃ§Ã£o

- Aplicar `sql/025_mercos_customer_document_index.sql` e `sql/027_mercos_customer_creation_pending_sync.sql`.
- Configurar `CUSTOMER_DOCUMENT_HMAC_KEY` com chave estÃ¡vel de ao menos 32 caracteres.
- Configurar MercosAdaptor e concluir customer baseline sync com a chave atual; manter sync recente.
- Somente apÃ³s validar esses itens, configurar `MERCOS_CUSTOMER_MUTATIONS_ENABLED=true`.
- NÃ£o copiar tokens Mercos para o XNamaiAgent. O caminho continua XNamaiAgent â†’ MercosAdaptor â†’ Mercos.
