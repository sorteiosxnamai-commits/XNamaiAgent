# XNamaiAgent Parte 1 — Neutralizar NewStore/Tray/Sorteio do Runtime

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remover do runtime do XNamaiAgent toda dependência executável de NewStore, Tray, sorteios e do banco de sorteio, substituindo a camada comercial por uma fronteira neutra cujo provider é explicitamente indisponível até a Parte 2 conectar Mercos.

**Architecture:** A pilha comercial já usa injeção de dependência (`ToolExecutor`). `sales_agent.py:108` importa `execute_tool` uma única vez e o repassa para cart/order/payment/shipping/checkout/product_retrieval — nenhum desses módulos importa Tray. Portanto existe um **ponto de estrangulamento único**: substituir `execute_tool`/`TOOL_SCHEMAS`/`TOOL_REGISTRY` por uma implementação neutra em `app/commerce/` neutraliza ~8500 LOC de serviços comerciais sem editá-los e sem reescrever `sales_agent.py`.

**Tech Stack:** Python 3.12, FastAPI, pydantic-settings 2.7.1, pytest, psycopg 3.

**Spec:** A especificação é a mensagem do usuário desta sessão (Parte 1 de 3). Não há arquivo de spec separado.

## Global Constraints

- `PERSONA_CHANGED=false`. Não alterar `persona NS.txt`, PersonaService, persona_repository, persona_models, instruction extensions, prompt comportamental, tom, estilo, guardrails comportamentais, reasoning, políticas conversacionais, memória.
- Não fazer busca-e-substituição em `openai_agent.py`, `sales_agent.py`, `prompt_compiler.py`, `response_critique.py`. Nesses arquivos só é permitido alterar **linhas de import** e chamadas técnicas, nunca texto de instrução.
- Não alterar `app/channels/ycloud_whatsapp.py`, assinatura YCloud, inbox/outbox, worker, idempotência, sender_key, rotas de webhook, Meta/Instagram.
- Não alterar OpenAI Responses API, `openai_gateway.py`, `openai_client.py`, model routing, LLM budgets, fallback, tool-call protocol.
- Não editar, apagar ou executar `sql/001` … `sql/022`.
- Sem commit, push, PR, deploy, alteração de Vercel/Render/Supabase, execução de SQL ou alteração de secrets.
- Não importar SDK Mercos, não usar tokens Mercos, não fazer chamadas HTTP Mercos. Nenhuma integração Mercos nesta parte.
- Sem fallback Tray. O comportamento correto na ausência de provider é `commerce_provider_unavailable`.
- Nomes de tools expostos ao modelo já são neutros (`search_products`, `get_product`, `check_inventory`, `search_customer`, `get_customer`, `list_orders`, `get_order`, `create_order`). Preservá-los.

---

### Task 1: Fronteira comercial neutra — erros e contratos

**Files:**
- Create: `app/commerce/__init__.py`
- Create: `app/commerce/errors.py`
- Create: `app/commerce/contracts.py`
- Test: `tests/test_commerce_boundary.py`

**Interfaces:**
- Consumes: nada.
- Produces: `CommerceUnavailableError`, `COMMERCE_UNAVAILABLE_CODE = "commerce_provider_unavailable"`, `CommerceProvider` (Protocol), `CommerceCapability` (enum de nomes de capacidade).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commerce_boundary.py
import pytest

from app.commerce.errors import COMMERCE_UNAVAILABLE_CODE, CommerceUnavailableError


def test_unavailable_code_is_stable():
    assert COMMERCE_UNAVAILABLE_CODE == "commerce_provider_unavailable"


def test_unavailable_error_carries_code_and_capability():
    err = CommerceUnavailableError("search_products")
    assert err.code == COMMERCE_UNAVAILABLE_CODE
    assert err.capability == "search_products"
    assert "commerce_provider_unavailable" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commerce_boundary.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.commerce'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/commerce/errors.py
from __future__ import annotations

COMMERCE_UNAVAILABLE_CODE = "commerce_provider_unavailable"


class CommerceUnavailableError(RuntimeError):
    """Nenhum provider comercial está configurado.

    Falha explícita e controlada: nunca inventar produto, preço, estoque
    ou pedido, e nunca cair de volta em um provider legado.
    """

    code = COMMERCE_UNAVAILABLE_CODE

    def __init__(self, capability: str = "") -> None:
        self.capability = capability
        suffix = f": {capability}" if capability else ""
        super().__init__(f"{COMMERCE_UNAVAILABLE_CODE}{suffix}")
```

```python
# app/commerce/contracts.py
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: Capacidades comerciais genéricas. Contratos — nem todas implementadas na Parte 1.
COMMERCE_CAPABILITIES: tuple[str, ...] = (
    "search_products",
    "get_product",
    "check_inventory",
    "search_customer",
    "get_customer",
    "create_customer",
    "update_customer",
    "list_orders",
    "get_order",
    "create_order",
    "update_order",
    "get_payment_conditions",
    "get_price_tables",
)


@runtime_checkable
class CommerceProvider(Protocol):
    """Fonte comercial oficial. Implementada por provider concreto."""

    name: str

    async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...
```

```python
# app/commerce/__init__.py
from .contracts import COMMERCE_CAPABILITIES, CommerceProvider
from .errors import COMMERCE_UNAVAILABLE_CODE, CommerceUnavailableError

__all__ = [
    "COMMERCE_CAPABILITIES",
    "CommerceProvider",
    "COMMERCE_UNAVAILABLE_CODE",
    "CommerceUnavailableError",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commerce_boundary.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Não commitar** (restrição global desta etapa)

---

### Task 2: NullCommerceProvider

**Files:**
- Create: `app/commerce/provider.py`
- Test: `tests/test_commerce_boundary.py` (adicionar)

**Interfaces:**
- Consumes: `CommerceProvider`, `CommerceUnavailableError`, `COMMERCE_CAPABILITIES`.
- Produces: `NullCommerceProvider`, `get_commerce_provider() -> CommerceProvider`, `set_commerce_provider(provider)`, `reset_commerce_provider()`.

- [ ] **Step 1: Write the failing test**

```python
# adicionar em tests/test_commerce_boundary.py
from app.commerce.provider import (
    NullCommerceProvider,
    get_commerce_provider,
    reset_commerce_provider,
    set_commerce_provider,
)


@pytest.mark.asyncio
async def test_null_provider_always_raises_unavailable():
    provider = NullCommerceProvider()
    with pytest.raises(CommerceUnavailableError) as exc:
        await provider.execute("search_products", {"query": "x"})
    assert exc.value.code == COMMERCE_UNAVAILABLE_CODE


def test_default_provider_is_null():
    reset_commerce_provider()
    assert isinstance(get_commerce_provider(), NullCommerceProvider)


def test_provider_is_replaceable():
    class FakeProvider:
        name = "fake"

        async def execute(self, capability, arguments):
            return {"ok": True, "capability": capability}

    try:
        set_commerce_provider(FakeProvider())
        assert get_commerce_provider().name == "fake"
    finally:
        reset_commerce_provider()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commerce_boundary.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.commerce.provider'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/commerce/provider.py
from __future__ import annotations

from typing import Any

from .contracts import CommerceProvider
from .errors import CommerceUnavailableError


class NullCommerceProvider:
    """Provider ativo da Parte 1: nenhuma fonte comercial configurada.

    Falha de modo explícito. Nunca consulta um provider legado e nunca
    inventa dados comerciais.
    """

    name = "null"

    async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise CommerceUnavailableError(capability)


_provider: CommerceProvider | None = None


def get_commerce_provider() -> CommerceProvider:
    global _provider
    if _provider is None:
        _provider = NullCommerceProvider()
    return _provider


def set_commerce_provider(provider: CommerceProvider) -> None:
    """Ponto de plug da Parte 2 (MercosCommerceProvider)."""
    global _provider
    _provider = provider


def reset_commerce_provider() -> None:
    global _provider
    _provider = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commerce_boundary.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Não commitar**

---

### Task 3: Tool registry neutro

**Files:**
- Create: `app/commerce/tools.py`
- Test: `tests/test_commerce_tools.py`

**Interfaces:**
- Consumes: `get_commerce_provider`, `CommerceUnavailableError`, `COMMERCE_UNAVAILABLE_CODE`.
- Produces: `TOOL_SCHEMAS` (lista de dicts OpenAI), `TOOL_REGISTRY` (dict com chave `commerce` apenas), `RETRYABLE_TOOL_NAMES`, `MUTATION_TOOL_NAMES`, `execute_tool(name, arguments) -> dict`.

Substitui `app.tray_tools`. Preserva os nomes de tools que já eram neutros. **Remove** da capability ativa: `raffle`, `list_coupons`, `get_coupon`, `create_cart`, `get_cart`, `get_cart_complete`, `set_cart_item_quantity`, `delete_cart`, `quote_shipping`, `list_shipping_methods`, `get_order_complete`, `get_order_payment`, `get_product_link`, `list_categories`, `get_category`, `get_category_tree`, `list_product_variants`, `get_product_variant` — todos específicos de Tray/NewStore.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commerce_tools.py
import pytest

from app.commerce.errors import COMMERCE_UNAVAILABLE_CODE
from app.commerce.tools import (
    MUTATION_TOOL_NAMES,
    RETRYABLE_TOOL_NAMES,
    TOOL_REGISTRY,
    TOOL_SCHEMAS,
    execute_tool,
)

NEUTRAL_NAMES = {
    "search_products",
    "get_product",
    "check_inventory",
    "search_customer",
    "get_customer",
    "list_orders",
    "get_order",
    "create_order",
}


def test_registry_has_no_raffle_domain():
    assert "raffle" not in TOOL_REGISTRY


def test_neutral_tool_names_are_preserved():
    assert NEUTRAL_NAMES.issubset(set(TOOL_REGISTRY["commerce"]))


def test_no_tray_or_newstore_specific_tools():
    forbidden = {"get_cart", "get_cart_complete", "create_cart", "list_coupons", "get_coupon"}
    assert forbidden.isdisjoint(set(TOOL_REGISTRY["commerce"]))


def test_schemas_are_openai_function_shaped():
    for item in TOOL_SCHEMAS:
        assert item["type"] == "function"
        assert item["function"]["name"] in TOOL_REGISTRY["commerce"]
        assert item["function"]["parameters"]["type"] == "object"


def test_read_only_and_mutation_are_disjoint_and_complete():
    assert RETRYABLE_TOOL_NAMES.isdisjoint(MUTATION_TOOL_NAMES)
    assert RETRYABLE_TOOL_NAMES | MUTATION_TOOL_NAMES == set(TOOL_REGISTRY["commerce"])


@pytest.mark.asyncio
async def test_execute_tool_returns_unavailable_without_raising():
    result = await execute_tool("search_products", {"query": "relogio"})
    assert result["ok"] is False
    assert result["error"] == COMMERCE_UNAVAILABLE_CODE


@pytest.mark.asyncio
async def test_execute_tool_never_invents_data():
    result = await execute_tool("get_product", {"product_id": "123"})
    assert "products" not in result
    assert "price" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commerce_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.commerce.tools'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/commerce/tools.py
from __future__ import annotations

from typing import Any

from .errors import COMMERCE_UNAVAILABLE_CODE, CommerceUnavailableError
from .provider import get_commerce_provider

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "search_products", "description": "Pesquisar produtos reais no catálogo oficial.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "name": {"type": "string"}, "reference": {"type": "string"}, "ean": {"type": "string"}, "brand": {"type": "string"}, "tokens": {"type": "array", "items": {"type": "string"}}, "available": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "page": {"type": "integer", "minimum": 1}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_product", "description": "Consultar detalhes atuais de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "check_inventory", "description": "Confirmar estoque e disponibilidade de um produto.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_customer", "description": "Pesquisar um cliente com filtro específico.", "parameters": {"type": "object", "properties": {"email": {"type": "string"}, "cpf": {"type": "string"}, "cnpj": {"type": "string"}, "name": {"type": "string"}, "limit": {"type": "integer", "maximum": 5}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_customer", "description": "Consultar um cliente identificado.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_orders", "description": "Listar pedidos do cliente.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "limit": {"type": "integer", "maximum": 20}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_order", "description": "Consultar um pedido.", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_payment_conditions", "description": "Consultar condições de pagamento disponíveis.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "order_id": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_price_tables", "description": "Consultar tabelas de preço aplicáveis.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "create_customer", "description": "Cadastrar um cliente.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "email": {"type": "string"}, "cpf": {"type": "string"}, "cnpj": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "update_customer", "description": "Atualizar dados de um cliente.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "name": {"type": "string"}, "email": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "create_order", "description": "Criar um pedido.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "items": {"type": "array", "items": {"type": "object"}}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "update_order", "description": "Atualizar um pedido.", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}},
]

RETRYABLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search_products",
        "get_product",
        "check_inventory",
        "search_customer",
        "get_customer",
        "list_orders",
        "get_order",
        "get_payment_conditions",
        "get_price_tables",
    }
)

MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {"create_customer", "update_customer", "create_order", "update_order"}
)

TOOL_REGISTRY: dict[str, tuple[str, ...]] = {
    "commerce": tuple(sorted(RETRYABLE_TOOL_NAMES | MUTATION_TOOL_NAMES)),
}


async def execute_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Executa uma capacidade comercial pelo provider configurado.

    Sem provider (Parte 1), devolve erro estruturado em vez de levantar,
    para que os chamadores existentes sigam tratando o resultado como dado.
    """
    args = dict(arguments or {})
    try:
        provider = get_commerce_provider()
        return await provider.execute(name, args)
    except CommerceUnavailableError as exc:
        return {"ok": False, "error": COMMERCE_UNAVAILABLE_CODE, "capability": exc.capability}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commerce_tools.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Não commitar**

---

### Task 4: Repontar os 7 importadores de tray_tools

**Files:**
- Modify: `app/capability_catalog.py:5`
- Modify: `app/openai_agent.py` (linha do import)
- Modify: `app/sales_agent.py:108`
- Modify: `app/response_critique.py` (linha do import)
- Modify: `app/commerce_router.py:7`
- Modify: `app/instagram_story_service.py` (linha do import)
- Modify: `app/product_image_index.py` (linha do import)
- Test: `tests/test_no_legacy_dependencies.py`

**Interfaces:**
- Consumes: `app.commerce.tools`.
- Produces: nenhum símbolo novo. Apenas troca a origem do import.

Somente a linha de import muda em cada arquivo. Nenhuma linha de instrução/persona é tocada.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_no_legacy_dependencies.py
import ast
import pathlib

import pytest

RUNTIME_DIRS = ("app", "api", "scripts")
FORBIDDEN_MODULES = {"tray_tools", "tray_adapter_client"}


def _runtime_python_files():
    for base in RUNTIME_DIRS:
        for path in pathlib.Path(base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.update(node.module.split("."))
    return found


@pytest.mark.parametrize("path", sorted(_runtime_python_files(), key=str), ids=str)
def test_runtime_does_not_import_legacy_commerce_modules(path):
    offending = _imported_modules(path) & FORBIDDEN_MODULES
    assert not offending, f"{path} ainda importa {sorted(offending)}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_no_legacy_dependencies.py -q`
Expected: FAIL em 7 arquivos de `app/` mais os que usam `tray_adapter_client`.

- [ ] **Step 3: Write minimal implementation**

Trocar em cada arquivo exatamente a linha de import:

```python
# antes
from .tray_tools import TOOL_REGISTRY, TOOL_SCHEMAS      # capability_catalog.py
from .tray_tools import TOOL_SCHEMAS, execute_tool       # openai_agent.py
from .tray_tools import execute_tool                     # sales_agent.py, response_critique.py,
                                                         # commerce_router.py, product_image_index.py
from .tray_tools import execute_tool as default_execute  # instagram_story_service.py

# depois
from .commerce.tools import TOOL_REGISTRY, TOOL_SCHEMAS
from .commerce.tools import TOOL_SCHEMAS, execute_tool
from .commerce.tools import execute_tool
from .commerce.tools import execute_tool as default_execute
```

- [ ] **Step 4: Run focused and related tests**

Run: `python -m pytest tests/test_no_legacy_dependencies.py tests/test_commerce_tools.py -q`
Expected: os 7 arquivos de `app/` passam; restam falhas apenas nos consumidores de `tray_adapter_client` (Task 5).

- [ ] **Step 5: Não commitar**

---

### Task 5: Neutralizar consumidores de TrayAdapterClient

**Files:**
- Modify: `app/pix_settlement.py:16,103`
- Modify: `app/remarketing.py:13,648`
- Modify: `api/index.py:52,457`
- Test: `tests/test_no_legacy_dependencies.py` (já escrito na Task 4)

**Interfaces:**
- Consumes: `CommerceUnavailableError`, `COMMERCE_UNAVAILABLE_CODE`.
- Produces: nenhum símbolo novo.

`pix_settlement.create_order` via Tray e o enriquecimento Tray em `remarketing` deixam de existir; o endpoint de diagnóstico Tray em `api/index.py:457` é removido.

- [ ] **Step 1: Verificar o teste da Task 4 ainda falhando nesses 3 arquivos**

Run: `python -m pytest tests/test_no_legacy_dependencies.py -q`
Expected: FAIL apenas em `app/pix_settlement.py`, `app/remarketing.py`, `api/index.py`.

- [ ] **Step 2: Substituir a criação de pedido Tray por indisponibilidade explícita**

Em `app/pix_settlement.py`, remover o import e trocar a chamada:

```python
# remover: from .tray_adapter_client import TrayAdapterClient, TrayAdapterError
from .commerce.errors import COMMERCE_UNAVAILABLE_CODE

# no lugar de: return await TrayAdapterClient().create_order(payload)
raise RuntimeError(COMMERCE_UNAVAILABLE_CODE)
```

- [ ] **Step 3: Remover o enriquecimento Tray do remarketing**

Em `app/remarketing.py`, remover o import e o bloco que instancia `TrayAdapterClient()` na linha 648, preservando o caminho em que o dado comercial simplesmente não está disponível.

- [ ] **Step 4: Remover o endpoint de diagnóstico Tray**

Em `api/index.py`, remover o import da linha 52 e o endpoint que chama `TrayAdapterClient().search_products(limit=1)` na linha 457, junto com sua rota.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_no_legacy_dependencies.py -q`
Expected: PASS para todos os arquivos runtime.

- [ ] **Step 6: Não commitar**

---

### Task 6: Deletar tray_tools.py e tray_adapter_client.py

**Files:**
- Delete: `app/tray_tools.py`
- Delete: `app/tray_adapter_client.py`
- Delete: `tests/test_tray_tools.py`
- Modify: `tests/evals/harness.py:82`
- Modify: `tests/test_hosted_order_payment.py`, `tests/test_payment_options_contract.py`, `tests/test_post_selection_commerce.py`, `tests/test_real_cart_checkout_regressions.py`

**Interfaces:**
- Consumes: `app.commerce.tools`.
- Produces: nada.

- [ ] **Step 1: Confirmar zero importadores runtime**

Run: `grep -rn --exclude-dir=__pycache__ "tray_tools\|tray_adapter_client" app api scripts`
Expected: nenhuma saída.

- [ ] **Step 2: Repontar os testes que ainda importam app.tray_tools**

Trocar `from app.tray_tools import ...` por `from app.commerce.tools import ...` nos 4 arquivos de teste e em `tests/evals/harness.py:82` (`import app.tray_tools as tray_tools` → `import app.commerce.tools as commerce_tools`, ajustando o patch alvo).

- [ ] **Step 3: Deletar os módulos e o teste dedicado ao provider legado**

```bash
rm app/tray_tools.py app/tray_adapter_client.py tests/test_tray_tools.py
```

`tests/test_tray_tools.py` cobre exclusivamente o adaptador Tray (`_reduce`, `search_products` sobre payload Tray). Não cobre comportamento genérico do agente — justificativa obrigatória no relatório.

- [ ] **Step 4: Run suite**

Run: `python -m pytest --ignore=tests/evals -q`
Expected: falhas restantes apenas em testes legacy de commerce, tratados na Task 12.

- [ ] **Step 5: Não commitar**

---

### Task 7: Capability catalog neutro

**Files:**
- Modify: `app/capability_catalog.py`
- Test: `tests/test_capability_catalog_neutral.py`

**Interfaces:**
- Consumes: `TOOL_REGISTRY`, `TOOL_SCHEMAS`, `RETRYABLE_TOOL_NAMES` de `app.commerce.tools`.
- Produces: `build_capability_catalog()` e `format_capability_catalog_for_prompt()` sem menção a fornecedor e sem domínio `raffle`.

A linha 90 (`"Usar APIs Tray para fatos comerciais; ..."`) é renderizada em três prompts (`sales_agent:825`, `sales_agent:1316`, `response_critique:601`). Trocar **apenas o nome do fornecedor**, preservando a estrutura e a força da regra anti-alucinação. `capability_catalog.py` é registro técnico de capacidades, não conteúdo de persona.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capability_catalog_neutral.py
from app.capability_catalog import (
    build_capability_catalog,
    format_capability_catalog_for_prompt,
)


def test_catalog_has_no_vendor_name():
    rendered = format_capability_catalog_for_prompt()
    for vendor in ("Tray", "tray", "NewStore", "newstore"):
        assert vendor not in rendered


def test_catalog_has_no_raffle_capabilities():
    catalog = build_capability_catalog()
    assert catalog.get("raffle_capabilities") in (None, [])
    assert "sorteio" not in format_capability_catalog_for_prompt().lower()


def test_anti_hallucination_rules_are_preserved():
    policies = " ".join(build_capability_catalog()["policy"])
    assert "Nunca inventar produto" in policies
    assert "fatos comerciais" in policies
    assert "payment_url" in policies


def test_retryable_classification_still_present():
    catalog = build_capability_catalog()
    by_name = {item["name"]: item for item in catalog["apis"]}
    assert by_name["search_products"]["retryable"] is True
    assert by_name["create_order"]["retryable"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_capability_catalog_neutral.py -q`
Expected: FAIL — `"Tray" in rendered` e `raffle_capabilities` ainda populado.

- [ ] **Step 3: Write minimal implementation**

Em `app/capability_catalog.py`: importar de `.commerce.tools`, remover o bloco `raffle` de `build_capability_catalog`, remover a linha `Capacidades de sorteio` de `format_capability_catalog_for_prompt`, atualizar `_API_HINTS` e `RETRYABLE_API_NAMES` para os nomes neutros, e trocar a política:

```python
# antes
"Usar APIs Tray para fatos comerciais; usar histórico/WORKING_MEMORY para continuidade",
# depois
"Usar a fonte comercial oficial para fatos comerciais; usar histórico/WORKING_MEMORY para continuidade",
```

As outras três políticas ficam idênticas.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_capability_catalog_neutral.py tests/test_response_critique.py -q`
Expected: PASS

- [ ] **Step 5: Não commitar**

---

### Task 8: Remover sorteio do runtime e o banco de sorteio

**Files:**
- Modify: `app/repository.py`
- Modify: `app/db.py:45-60`
- Modify: `app/config.py:660,1004-1010`
- Modify: `api/index.py:379-382`
- Modify: `app/ingress/worker.py:27-29`
- Delete: `app/simulation.py` (se exclusivo de sorteio — confirmar importadores antes)
- Test: `tests/test_no_legacy_dependencies.py` (estender)

**Interfaces:**
- Consumes: nada novo.
- Produces: `app.repository.normalize_phone` preservado; `find_customer_profile_by_phone` devolve `{"found": False, ...}` sem tocar banco externo.

`repository.py` acessa somente `users`, `draws`, `draw`, `raffles`, `payments`, `app_config_new` — todas do domínio sorteio/NewStore. YCloud usa apenas `normalize_phone` (utilitário puro).

- [ ] **Step 1: Write the failing test**

```python
# adicionar em tests/test_no_legacy_dependencies.py
import pathlib


def test_runtime_has_no_sorteio_database_dependency():
    offenders = []
    for base in ("app", "api", "scripts"):
        for path in pathlib.Path(base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "SORTEIO_DATABASE_URL" in text or "get_sorteio_conn" in text:
                offenders.append(str(path))
    assert not offenders, f"dependência de banco de sorteio em: {offenders}"


def test_normalize_phone_still_available():
    from app.repository import normalize_phone

    assert normalize_phone("+55 (11) 99999-9999")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_no_legacy_dependencies.py -q -k sorteio`
Expected: FAIL listando `app/config.py`, `app/db.py`, `app/repository.py`, `api/index.py`.

- [ ] **Step 3: Remover acesso ao banco de sorteio**

Em `app/db.py`: remover `get_sorteio_conn` e o import de `resolved_sorteio_database_url`.
Em `app/config.py`: remover o campo `sorteio_database_url` (linha 660) e a função `resolved_sorteio_database_url` (1004-1010).
Em `app/repository.py`: manter `normalize_phone`; substituir as funções que consultam `users`/`draws`/`raffles`/`payments` por retorno "não disponível" ou removê-las junto com seus chamadores.
Em `api/index.py`: remover `sorteio_database_configured` / `sorteio_database_dedicated` do health.
Em `app/ingress/worker.py:27-29`: substituir a chamada por `{"found": False, ...}`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_no_legacy_dependencies.py tests/test_ycloud_webhook_route.py tests/test_ycloud_whatsapp_adapter.py -q`
Expected: PASS — YCloud intacto.

- [ ] **Step 5: Não commitar**

---

### Task 9: Auditar e neutralizar PIX/Mercado Pago

**Files:**
- Analisar: `app/mercadopago_client.py`, `app/pix_payment_service.py`, `app/pix_payment_repository.py`, `app/pix_checkout_service.py`, `app/pix_settlement.py`, `app/pix_webhook_api.py`
- Modify: `api/index.py` (remover router PIX), `app/sales_agent.py` (import de `pix_checkout_service`)
- Test: `tests/test_no_legacy_dependencies.py` (estender)

**Interfaces:**
- Consumes: nada novo.
- Produces: nenhuma rota PIX ativa.

Critério: PIX existe hoje exclusivamente para o checkout Tray/NewStore (`pix_settlement` criava pedido via `TrayAdapterClient`). Classificação: `NEWSTORE_SPECIFIC` → remover do runtime. Não conectar pagamento novo.

- [ ] **Step 1: Provar a classificação**

Run: `grep -rn "pix_\|mercadopago" app api --include=*.py | grep -v "^app/pix_\|^app/mercadopago"`
Registrar no relatório cada consumidor e por que é específico de NewStore.

- [ ] **Step 2: Write the failing test**

```python
def test_no_active_pix_routes():
    import api.index as index

    paths = {getattr(r, "path", "") for r in index.app.routes}
    assert not [p for p in paths if "payments" in p or "pix" in p]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_no_legacy_dependencies.py -q -k pix`
Expected: FAIL — rota `/api/payments/webhook` presente.

- [ ] **Step 4: Remover do runtime**

Remover o `include_router(pix_payments_router)` de `api/index.py`, o import de `pix_checkout_service` em `sales_agent.py` e seus call sites, e deletar os 6 módulos PIX/MP após confirmar zero importadores.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_no_legacy_dependencies.py -q`
Expected: PASS

- [ ] **Step 6: Não commitar**

---

### Task 10: Knowledge e brand

**Files:**
- Modify: `app/site_knowledge.py`, `app/store_knowledge.py`, `app/vip_profiles.py`
- Test: `tests/test_knowledge_not_configured.py`

**Interfaces:**
- Consumes: nada novo.
- Produces: funções de knowledge devolvem "não configurado" em vez de dados NewStore.

`store_knowledge.py` não tem importador runtime → deletar. `site_knowledge.py` é importado por `agent_replies`, `guardrails`, `handoff_service`, `openai_agent`, `response_critique`, `simulation` → **neutralizar conteúdo**, preservar a interface. Não inventar conteúdo XNamai.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_knowledge_not_configured.py
import pathlib


def test_site_knowledge_has_no_newstore_urls():
    text = pathlib.Path("app/site_knowledge.py").read_text(encoding="utf-8")
    for token in ("sorteionewstore", "newstorerj", "NewStore", "newstore"):
        assert token not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_knowledge_not_configured.py -q`
Expected: FAIL — URLs NewStore presentes.

- [ ] **Step 3: Neutralizar**

Substituir constantes de URL/contato/VIP por vazio ou `None`, mantendo assinaturas. Deletar `app/store_knowledge.py` (sem importadores).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_knowledge_not_configured.py -q`
Expected: PASS

- [ ] **Step 5: Não commitar**

---

### Task 11: Identidade técnica e config

**Files:**
- Modify: `app/config.py` (defaults e remoção de envs Tray)
- Modify: `.env.example`
- Test: `tests/test_config_defaults.py` (estender), `tests/test_no_legacy_dependencies.py`

**Interfaces:**
- Consumes: nada novo.
- Produces: `APP_NAME` default `XNamaiAgent`; envs `TRAY_*` e `SORTEIO_DATABASE_URL` ausentes.

Trocar apenas identidade técnica: `APP_NAME`. **Não** trocar `AGENT_PERSONA_TENANT_ID` (`newstore`) nem `AGENT_PERSONA_KEY` (`newstore_commercial`) — são chaves de lookup em `ai_agent_persona_versions`; trocá-las sem migrar as linhas faria a persona ativa não ser encontrada. Registrar como `PERSONA_PROTECTED_RESIDUE`.

- [ ] **Step 1: Write the failing test**

```python
def test_app_name_default_is_xnamai():
    from app.config import Settings

    assert Settings.model_fields["app_name"].default == "XNamaiAgent"


def test_no_tray_env_fields():
    from app.config import Settings

    tray_fields = [n for n in Settings.model_fields if "tray" in n.lower()]
    assert tray_fields == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_defaults.py -q -k "xnamai or tray"`
Expected: FAIL — default `NewStoreAgent` e campos `tray_adapter_*` presentes.

- [ ] **Step 3: Aplicar**

Trocar o default de `app_name`; remover os campos `tray_adapter_url`, `tray_adapter_token`, `tray_tools_enabled` e o `sorteio_database_url`; remover as entradas correspondentes do `.env.example`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_defaults.py tests/test_config_empty_env.py -q`
Expected: PASS

- [ ] **Step 5: Não commitar**

---

### Task 12: Triagem dos testes legados e verificação final

**Files:**
- Delete/Modify: testes classificados como `LEGACY_TRAY_TEST`, `LEGACY_NEWSTORE_TEST`, `LEGACY_RAFFLE_TEST`
- Test: suíte completa

**Interfaces:**
- Consumes: tudo anterior.
- Produces: suíte verde.

Classificar cada teste que falhar em A/B/C/D/E. `AI_CORE_TEST` e `PERSONA_TEST` **devem continuar passando** e não podem ser reescritos para mascarar mudança. Só remover um teste junto com a feature que ele cobria, e provar no relatório que não cobre comportamento genérico.

- [ ] **Step 1: Levantar falhas e classificar**

Run: `python -m pytest --ignore=tests/evals -q 2>&1 | tail -60`
Registrar cada arquivo com sua classificação e justificativa.

- [ ] **Step 2: Remover testes de features removidas**

Apenas os que cobrem exclusivamente Tray/NewStore/sorteio.

- [ ] **Step 3: Verificação completa**

```bash
python -m pytest --ignore=tests/evals -q
python -m pytest -m offline_eval -q
python -m pytest tests/evals -q
python -m compileall -q app api scripts
python scripts/scan_secrets.py
python scripts/package_release.py --dry-run
python -c "import sys; sys.path.insert(0,'.'); import api.index; print('import OK')"
```

- [ ] **Step 4: Análise estática final**

```bash
grep -rn --exclude-dir=__pycache__ -iE "tray|newstore" app api scripts
grep -rn --exclude-dir=__pycache__ -iE "raffle|sorteio" app api scripts
```
Toda ocorrência restante precisa estar classificada como `PERSONA_PROTECTED_RESIDUE`.

- [ ] **Step 5: Não commitar** — entregar o relatório final.
