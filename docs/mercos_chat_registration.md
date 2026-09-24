# Cadastro Mercos pelo ChatBô

Contrato conferido em 2026-09-24: [clientes Mercos](https://docs.mercos.com/reference/v1clientes). O POST exige `razao_social` e `tipo` por padrão; cada conta pode exigir campos adicionais. CNPJ alfanumérico segue o [cálculo oficial do DV da Receita Federal](https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/documentos-tecnicos/cnpj/manual-dv-cnpj.pdf).

O ChatBô coleta CPF/CNPJ, nome legal, e-mail e telefone em turnos curtos. Pessoa física usa `tipo=F` e o nome completo como `razao_social`. Pessoa jurídica usa `tipo=J`, pergunta nome fantasia de forma opcional e nunca usa o nome do WhatsApp como razão social. Telefone válido do WhatsApp preenche o rascunho e pode ser corrigido. O POST exige revisão, confirmação explícita e lookup `NOT_FOUND`.

No boundary do MercosAdaptor, `document` vira `cnpj`; `email` vira `emails: [{"email": ...}]`; `phone` vira `telefones: [{"numero": ...}]`. Campos oficiais adicionais só são enviados quando coletados: `nome_fantasia`, `inscricao_estadual`, `suframa`, `cep`, `rua`, `numero`, `complemento`, `bairro`, `cidade`, `estado`, `observacao`. A observação é omitida por padrão. Se o cliente informar CEP e a integração já configurada resolver o endereço, rua, bairro, cidade e estado são preenchidos sem inventar número ou complemento.

O parser determinístico dos erros 422 aceita apenas nomes de campos oficiais reconhecidos e mensagens de obrigatoriedade. O claim é liberado após rejeição definitiva; o ChatBô pede o campo, apresenta nova revisão e exige nova confirmação. Duplicidade explícita dispara sync e lookup; se não resolver, handoff. Timeout, conexão interrompida e 5xx deixam o claim `unknown` e proíbem retry.

Resposta 2xx sem ID é criação confirmada. O claim passa a `created_pending_sync`, mantém o segundo POST bloqueado e solicita customer sync. O índice HMAC local associa o documento ao `customer_id` quando a listagem Mercos incluir o novo cliente; então o claim vira `created`. Se o sync não encontrar imediatamente, a conversa permanece pendente até a próxima sincronização. O índice conserva isolamento por tenant, baseline completo e recente, fingerprint da chave, advisory lock e claim durável. Documento bruto não é gravado nele.

## Antes de ligar em produção

- Aplicar `sql/025_mercos_customer_document_index.sql` e `sql/026_mercos_customer_creation_pending_sync.sql`.
- Configurar `CUSTOMER_DOCUMENT_HMAC_KEY` com chave estável de ao menos 32 caracteres.
- Configurar MercosAdaptor e concluir customer baseline sync com a chave atual; manter sync recente.
- Somente após validar esses itens, configurar `MERCOS_CUSTOMER_MUTATIONS_ENABLED=true`.
- Não copiar tokens Mercos para o XNamaiAgent. O caminho continua XNamaiAgent → MercosAdaptor → Mercos.
