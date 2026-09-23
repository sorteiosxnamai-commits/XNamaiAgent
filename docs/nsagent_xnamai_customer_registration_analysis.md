# Comparação local: base NSAgent e XnamaiAgent

Data da revisão: 2026-09-22.

## Método

A comparação usa somente fontes locais:

- base anterior à migração de identidade, preservada no histórico Git;
- implementação atual do XnamaiAgent;
- contrato local do MercosAdaptor;
- testes offline com a chave da OpenAI vazia.

Nenhuma mensagem, tool ou teste desta análise chama OpenAI, Mercos ou canais de atendimento.

## Resultado da comparação

| Área | Base NSAgent | XnamaiAgent após esta revisão |
|---|---|---|
| Identidade | Persona ampla e especializada no negócio anterior | Identidade exclusiva da Xnamai e catálogo oficial |
| Descoberta de produto | Boa interpretação semântica, dependente da categoria anterior | Busca por cabos, carregadores, fones, capas, suportes, hubs, conectores e compatibilidade |
| Continuidade | Referências e preferências conversacionais | Mantém produto, lista, mídia, carrinho, checkout, pedido e cadastro entre turnos |
| Fonte comercial | Contrato genérico com várias capacidades | MercosAdaptor, catálogo local sincronizado e fatos com validade |
| Entrega | Fluxo direto | Inbox/outbox duráveis, deduplicação, locks e workers |
| Cliente | Consulta e estrutura de checkout; mutação existia apenas no cliente técnico | Cadastro conversacional determinístico com coleta, validação, revisão, confirmação e criação protegida por configuração |
| Segurança da mutação | Cliente HTTP não repetia POST ambíguo | Mantida; resultado incerto bloqueia reenvio e exige verificação humana |
| Testes sem IA | Cobertura ampla, mas orientada ao negócio anterior | Cenários Xnamai, fluxo de cadastro e regressão completa executáveis sem OpenAI |

## Fluxo de cadastro implementado

1. O cliente pede para se cadastrar.
2. O agente envia um modelo curto para PF ou PJ.
3. O cliente informa nome ou razão social, nome fantasia, CPF/CNPJ, e-mail e telefone.
4. O agente valida CPF, CNPJ, e-mail e telefone sem IA.
5. O agente mostra uma revisão com documento e contato mascarados.
6. Somente a frase explícita de confirmação libera uma chamada `create_customer`.
7. A chamada é enviada uma única vez. Timeout ou resposta ambígua nunca provoca repetição automática.
8. O identificador confirmado do cliente fica associado ao estado comercial da conversa.

O recurso fica desativado por padrão. Para habilitá-lo em um ambiente já homologado:

```text
MERCOS_CUSTOMER_MUTATIONS_ENABLED=true
```

## Lacunas restantes antes da ativação em produção

1. **Retorno do identificador:** homologar se o MercosAdaptor devolve o ID criado no corpo. A API de origem também pode devolvê-lo em cabeçalho; o adaptador atual não preserva esse cabeçalho quando a resposta não tem corpo.
2. **Prevenção de duplicidade:** criar índice local de clientes ou busca por CPF/CNPJ. A listagem atual aceita apenas cursor incremental, então o agente ainda não consegue verificar cadastro existente antes do POST.
3. **Regras comerciais:** confirmar com a Xnamai quais tipos de pessoa são aceitos, campos obrigatórios, categorias, tabelas de preço, condição de pagamento e bloqueios B2B.
4. **Endereço:** o cadastro inicial coleta identificação e contato. Endereço completo já existe no checkout, mas precisa de mapeamento homologado antes de entrar no payload de cliente.
5. **Atualização cadastral:** permanece desabilitada. Alterar cadastro existente exige identificação forte, revisão específica e trilha de auditoria separada.
6. **Operação:** ativar primeiro em homologação, observar sucesso, rejeições e estados ambíguos; depois liberar gradualmente em produção.

## Critérios de aceite para produção

- nenhuma criação sem confirmação explícita;
- nenhuma chamada duplicada após timeout;
- nenhum dado pessoal completo em logs ou mensagens de revisão;
- ID do cliente retornado e persistido;
- CPF/CNPJ já existente não cria duplicidade;
- erros de validação da Mercos voltam como correção objetiva de campo;
- cadastro criado pode ser usado na revisão do pedido sem nova coleta.
