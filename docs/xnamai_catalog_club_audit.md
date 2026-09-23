# Auditoria local: catálogo Xnamai, Club e legado NSAgent

Data: 2026-09-22.

## Fontes verificadas

- catálogo B2B oficial: `https://xnamai.meuspedidos.com.br/`;
- site institucional: `https://www.xnamai.com/`;
- Club oficial: `https://www.clubxnamai.com.br/`;
- API pública de planos usada pelo site do Club;
- índice local sincronizado pelo MercosAdaptor;
- base NSAgent preservada no histórico Git antes da migração para Xnamai.

## Busca de produtos

O portal B2B exige cadastro e autenticação. Por isso o agente não extrai produtos
da página pública. A fonte correta é o catálogo Mercos sincronizado para o índice
local. A busca aplica texto, nome, referência, disponibilidade, paginação e pista
de marca. Preço e estoque carregam validade própria; dado vencido ou ausente não
é apresentado como atual. Produtos explicitamente inativos ou excluídos não são
oferecidos.

Os testes offline cobrem consulta, paginação, referência, marca, preço, estoque,
seleção entre resultados, indisponibilidade do provider e catálogo ainda não
sincronizado.

Na máquina desta auditoria, `MERCOS_ADAPTOR_URL`, `MERCOS_ADAPTOR_API_KEY` e a
prontidão do índice não estão configurados. Portanto, não foi possível consultar
linhas reais do catálogo nesse ambiente. O agente agora trata essa condição sem
falso negativo: informa que a consulta está indisponível e entrega os links do
catálogo oficial e do Club, sem dizer que o produto não existe.

## XNaMai Club

O site oficial informa que membros com assinatura ativa acessam preços
exclusivos em compras elegíveis. O agente agora:

1. responde perguntas sobre o Club sem usar modelo;
2. diferencia declaração de membro e não membro;
3. oferece o Club uma vez, com frase condicional, quando o status é desconhecido;
4. não repete a oferta para quem já se declarou membro;
5. direciona plano e regras atuais ao site oficial;
6. não promete economia, cashback ou combinação de descontos individual.

O agente não consulta a conta do Club porque ainda não existe vínculo autenticado
entre a conversa e o backend do Club. Até essa integração existir, usa a forma
segura: “se ainda não for membro”.

## Comparação com NSAgent

A base NSAgent tinha boa continuidade e busca semântica, mas estava especializada
no negócio anterior. O XnamaiAgent mantém as técnicas úteis de continuidade,
ranking, confirmação factual e recuperação de contexto, aplicadas ao catálogo
Mercos da Xnamai. Nenhuma regra, marca ou categoria herdada do agente anterior aparece
na superfície ativa de prompt, persona ou conhecimento institucional.
