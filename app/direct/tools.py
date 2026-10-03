"""Explicit read-only capabilities; the model cannot choose backend operations."""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.persona_knowledge import retrieve_knowledge


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Search(Arguments):
    query: str = Field(max_length=300)
    offset: int = Field(ge=0, le=10000)
    limit: int = Field(ge=1, le=20)


class Compare(Arguments):
    query: str = Field(max_length=300)


class Product(Arguments):
    product_id: str = Field(min_length=1, max_length=100)


class Knowledge(Arguments):
    query: str = Field(min_length=1, max_length=1000)


class Human(Arguments):
    pass


DEFINITIONS = {
    "search_products": (Search, "Busca no catálogo sincronizado Mercos. Paginação por offset; query vazia lista o catálogo. Não representa o total."),
    "compare_products": (Compare, "Lê todos os candidatos da query para comparar, até 500. Se ultrapassar o limite, refine a busca; não retorna uma seleção parcial como completa."),
    "get_product": (Product, "Consulta detalhes e validade de preço e estoque pelo ID Mercos."),
    "check_inventory": (Product, "Consulta estoque e sua validade; dado ausente ou vencido não é estoque zero."),
    "search_knowledge": (Knowledge, "Consulta documentos aprovados e vigentes da persona publicada."),
    "request_human": (Human, "Sinaliza solicitação explícita de atendimento humano. Não confirma atribuição de atendente."),
}


def tool_schemas():
    return [{"type": "function", "name": name, "description": description,
             "parameters": model.model_json_schema(), "strict": True}
            for name, (model, description) in DEFINITIONS.items()]


class DirectTools:
    def __init__(self, *, provider, incoming, documents):
        self.provider = provider
        self.incoming = incoming
        self.documents = documents
        self.handoff = False
        self.calls = 0
        self.products = []

    async def _commerce(self, name, arguments):
        # Existing provider performs synchronous DB reads. Keep them off the
        # request event loop, without exposing its mutating execute interface.
        def read():
            if not self.provider.available:
                return {"ok": False, "error": "catalog_unavailable"}
            return asyncio.run(self.provider.execute(name, arguments))
        return await asyncio.to_thread(read)

    async def execute(self, name, arguments):
        self.calls += 1
        if name not in DEFINITIONS:
            return {"ok": False, "error": "tool_not_allowed"}
        try:
            args = DEFINITIONS[name][0].model_validate_json(arguments).model_dump()
        except (ValidationError, ValueError, TypeError):
            return {"ok": False, "error": "invalid_arguments"}
        try:
            if name == "search_knowledge":
                return {"ok": True, "documents": retrieve_knowledge(self.documents, args["query"])}
            if name == "request_human":
                from app.guardrails import detect_human_support_request
                if not detect_human_support_request(self.incoming.text):
                    return {"ok": False, "error": "explicit_human_request_required"}
                self.handoff = True
                return {"ok": True, "status": "requested", "assigned": False}
            if name == "compare_products":
                return await self._compare(args["query"])
            if name == "search_products":
                args["strict"] = True
            result = await self._commerce(name, args)
            products = result.get("products") or ([result["product"]] if result.get("product") else [])
            for product in products:
                identity = {key: product.get(key) for key in ("id", "reference", "name")}
                self.products = [p for p in self.products if p.get("id") != identity["id"]]
                self.products.append(identity)
            self.products = self.products[-60:]
            return result
        except Exception:
            # No exception strings: providers can include sensitive payloads.
            return {"ok": False, "error": "tool_unavailable"}

    async def _compare(self, query):
        candidates, seen = [], set()
        async with asyncio.timeout(25):
            for offset in range(0, 500, 20):
                page = await self._commerce("search_products", {
                    "query": query, "strict": True, "offset": offset, "limit": 20})
                if not page.get("ok"):
                    return {"ok": False, "error": "catalog_comparison_unavailable"}
                products = page.get("products") or []
                for product in products:
                    identity = product.get("id")
                    if not identity or identity in seen:
                        return {"ok": False, "error": "catalog_changed_retry_search"}
                    seen.add(identity)
                    candidates.append({key: product.get(key) for key in ("id", "reference", "name")})
                more = page.get("paging", {}).get("has_more")
                if more is False:
                    return {"ok": True, "candidates": candidates, "count": len(candidates),
                            "complete": True, "source": "mercos_synced_catalog",
                            "limitations": "Leitura paginada; o catálogo pode mudar durante o sync. Consulte detalhes dos escolhidos antes de afirmar preço ou estoque."}
                if more is not True or len(products) != 20:
                    return {"ok": False, "error": "catalog_pagination_incomplete"}
        return {"ok": False, "error": "too_many_candidates", "limit": 500,
                "instruction": "Refine a query; não foi feita comparação completa."}
