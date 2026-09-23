# Identidade institucional (`<business_identity>`)

## Decisão

O XNamaiAgent é **single-brand por implantação**. O bloco `<business_identity>`
(montado em `app/prompt_compiler.py` a partir de `app/site_knowledge.py`) fixa
a Xnamai e seus canais oficiais **de propósito** e permanece como está.

## Evidência

| Pergunta | Resposta no código |
| --- | --- |
| Existe multi-tenancy de verdade? | Não em runtime. Cada processo resolve **um** tenant por configuração: `AGENT_PERSONA_TENANT_ID`, `COMMERCE_TENANT_ID` e o mapa `INSTAGRAM_STORY_ACCOUNT_TENANT_MAP` (que, por conveniência single-store, aponta a conta IG para o tenant da persona). Nenhuma mensagem escolhe tenant. |
| O que `tenant_id` representa? | Um **namespace de isolamento de dados**: versões de persona, extensões, memórias, índice de catálogo, Stories e estado de sincronização Mercos são particionados por ele. Não é um cliente/workspace servido pela mesma implantação. |
| A quem pertence a identidade? | À **configuração institucional** da implantação (nome da empresa, site, catálogo oficial). Não é tom nem estilo — por isso não pertence à persona — e é fato público verificável — por isso não fica a critério do GPT. |

## Separação de autoridade

- **Persona publicada (GPT/banco):** tom, estilo, forma de apresentar, voz.
- **`<business_identity>` (código/config):** qual empresa é representada e quais
  URLs são oficiais. Prevalece sobre persona, memória e histórico para impedir
  que conteúdo externo troque a empresa representada.
- **`<fixed_safety_policy>`:** regras imutáveis de segurança/fato.

## Se o produto virar multi-tenant

Uma mesma implantação atendendo várias marcas exigiria, antes de qualquer
mudança neste bloco:

1. resolver o tenant por canal/credencial em cada entrada (como já faz
   `app/story_tenant.py` para Stories) e propagá-lo no `TurnRuntimeContext`;
2. mover `SITE_URL`/`STORE_URL` e o texto institucional para configuração por
   tenant (por exemplo `metadata.knowledge_documents` da persona publicada ou
   uma tabela de identidade por tenant), com fallback **fail-closed**;
3. manter `<business_identity>` como camada separada da persona, agora
   parametrizada pelo tenant resolvido.

Nada disso é necessário enquanto cada implantação servir uma única marca.
