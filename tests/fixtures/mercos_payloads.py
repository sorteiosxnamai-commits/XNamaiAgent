"""Fixtures SINTETICAS baseadas na documentacao oficial da Mercos.

Nenhum dado real. Documentos, e-mails e telefones sao obviamente falsos
(`00000000000000`, `@example.invalid`). Os NOMES DOS CAMPOS vem da documentacao
oficial — produtos e clientes na API v1, pedidos na v2 — e foram CONFERIDOS
contra o adaptador de producao (500 produtos, 500 clientes, 20 pedidos).

Ajustes feitos apos essa conferencia:

* `categoria_id` NAO existe no payload real (0/500) e saiu da fixture de produto;
* `codigo` vem vazio em boa parte do catalogo -> ha helper para esse caso;
* `saldo_estoque` vem `null` com frequencia -> ha helper para esse caso;
* `status`/`status_faturamento` de pedido chegam como STRING numerica ("0"/"2"),
  nao inteiro.
"""

from __future__ import annotations

from typing import Any


def product(**overrides: Any) -> dict[str, Any]:
    """Produto completo. `overrides=None` remove o campo (testa ausencia)."""
    row = {
        "id": 1001,
        "codigo": "REF-1001",
        "nome": "Produto Sintetico A",
        "preco_tabela": 149.90,
        "preco_minimo": 120.00,
        "unidade": "UN",
        "saldo_estoque": 7,
        "observacoes": "",
        "ultima_alteracao": "2026-03-01T10:00:00",
        "excluido": False,
        "ativo": True,
        "moeda": "BRL",
        "multiplo": 1,
        "peso_bruto": 0.5,
        "largura": 10,
        "altura": 5,
        "comprimento": 15,
        "exibir_no_b2b": 1,
        "precos_especificos": False,
        "tipo_ipi": "",
    }
    return _apply(row, overrides)


def product_without_reference(**overrides: Any) -> dict[str, Any]:
    """Produto com `codigo` vazio — comum no catalogo real."""
    return product(codigo="", **overrides)


def product_without_stock(**overrides: Any) -> dict[str, Any]:
    """Produto sem `saldo_estoque` — comum no catalogo real."""
    return product(saldo_estoque=None, **overrides)


def product_inactive(**overrides: Any) -> dict[str, Any]:
    return product(ativo=False, excluido=False, **overrides)


def product_excluded(**overrides: Any) -> dict[str, Any]:
    return product(ativo=True, excluido=True, **overrides)


def customer(**overrides: Any) -> dict[str, Any]:
    """Cliente completo. PII sinteticamente falsa, nunca real."""
    row = {
        "id": 2002,
        "razao_social": "Empresa Sintetica LTDA",
        "nome_fantasia": "Empresa Sintetica",
        "tipo": "PJ",
        "cnpj": "00000000000000",
        "inscricao_estadual": "ISENTO",
        "emails": ["contato@example.invalid"],
        "telefones": ["+550000000000"],
        "contatos": [],
        "rua": "Rua Sintetica",
        "numero": "1",
        "complemento": "",
        "cep": "00000000",
        "bairro": "Centro",
        "cidade": "Cidade Sintetica",
        "estado": "SP",
        "segmento_id": 3,
        "rede_id": None,
        "bloqueado_b2b": False,
        "bloqueado": False,
        "motivo_bloqueio_id": None,
        "excluido": False,
        "ultima_alteracao": "2026-03-01T11:00:00",
    }
    return _apply(row, overrides)


def order_item(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 5001,
        "produto_id": 1001,
        "produto_codigo": "REF-1001",
        "produto_nome": "Produto Sintetico A",
        "tabela_preco_id": 9,
        "preco_tabela": 149.90,
        "preco_liquido": 139.90,
        "quantidade": 2,
        "subtotal": 279.80,
        "observacoes": "",
        "excluido": False,
    }
    return _apply(row, overrides)


def order(**overrides: Any) -> dict[str, Any]:
    """Pedido completo (API v2)."""
    row = {
        "id": 3003,
        "numero": "PED-3003",
        # STRING numerica, como a Mercos realmente devolve.
        "status": "2",
        "status_faturamento": "0",
        "status_custom_id": None,
        "valor_frete": 25.00,
        "total": 304.80,
        "rastreamento": "",
        "observacoes": "",
        "data_emissao": "2026-03-01T12:00:00",
        "data_criacao": "2026-03-01T11:55:00",
        "prazo_entrega": 10,
        "modalidade_entrega_nome": "CIF",
        "possui_informacao_pagamento": True,
        "cliente_id": 2002,
        "cliente_razao_social": "Empresa Sintetica LTDA",
        "cliente_nome_fantasia": "Empresa Sintetica",
        "cliente_cnpj": "00000000000000",
        "data_criacao": None,
        "valor_frete": None,
        "transportadora_id": 44,
        "transportadora_nome": "Transportadora Sintetica",
        "condicao_pagamento": "30/60",
        "condicao_pagamento_id": 12,
        "forma_pagamento_id": 4,
        "tipo_pedido_id": 1,
        "itens": [order_item()],
    }
    return _apply(row, overrides)


def _apply(row: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Aplica overrides. Valor ``...`` (Ellipsis) REMOVE a chave."""
    for key, value in overrides.items():
        if value is ...:
            row.pop(key, None)
        else:
            row[key] = value
    return row
