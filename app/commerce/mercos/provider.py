"""`MercosCommerceProvider`: a fronteira onde tudo que sabe de Mercos termina.

Regra de arquitetura: nenhum modulo fora de `app/commerce/mercos/` pode conter
`if provider == "mercos"`. O AI core fala capability; quem traduz e este arquivo.

Matriz de capacidades — derivada do contrato, nao de suposicao
--------------------------------------------------------------
O contrato do adaptador (lido em @38f0bf4) aceita, em listagem, **apenas** o
filtro `alterado_apos`. Nao existe busca textual, filtro por documento, nem
carrinho, cupom, cotacao de frete ou link de pagamento. Isso derruba boa parte
do contrato herdado, que foi desenhado para outro fornecedor.

Cada capacidade abaixo carrega o motivo da sua classificacao. Uma capacidade so
e exposta ao modelo quando o provider consegue devolver **fato verificado** — na
duvida ela fica fora, porque um fato comercial inventado chega ao cliente como
se fosse oficial.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import CommerceUnavailableError
from .client import MercosAdaptorClient, MercosAdaptorError

PROVIDER_NAME = "mercos"


@dataclass(frozen=True)
class CapabilitySupport:
    """Como uma capacidade do contrato se comporta neste provider."""

    capability: str
    strategy: str
    endpoint: str | None
    exposed_to_llm: bool
    reason: str

    @property
    def supported(self) -> bool:
        return self.strategy != "UNSUPPORTED"


def _cap(capability, strategy, endpoint, exposed, reason) -> CapabilitySupport:
    return CapabilitySupport(capability, strategy, endpoint, exposed, reason)


#: Matriz completa. Toda capacidade do contrato neutro aparece aqui, inclusive
#: as nao suportadas — silencio seria indistinguivel de esquecimento.
CAPABILITY_MATRIX: tuple[CapabilitySupport, ...] = (
    # --- catalogo ---------------------------------------------------------
    _cap(
        "search_products",
        "SUPPORTED_LOCAL",
        "GET /v1/products?alterado_apos= (sync) -> ai_catalog_index",
        True,
        "o adaptador nao tem busca textual; o sync incremental alimenta o indice "
        "local e a busca acontece nele. Filtros brand/ean sao recusados "
        "explicitamente por nao terem mapeamento nesta integracao",
    ),
    _cap(
        "get_product",
        "SUPPORTED_LOCAL",
        "ai_catalog_index (snapshot do sync)",
        True,
        "o indice local e a fonte principal: o GET por id do adaptador pode "
        "responder 403 em producao e nao serve de requisito. A validade do "
        "snapshot viaja junto com o fato",
    ),
    _cap(
        "get_product_link",
        "UNSUPPORTED",
        None,
        False,
        "a Mercos e B2B por pedido; nao ha vitrine com URL publica de produto",
    ),
    _cap(
        "check_inventory",
        "SUPPORTED_LOCAL",
        "ai_catalog_index.stock (<- saldo_estoque)",
        True,
        "`saldo_estoque` e campo oficial. A resposta distingue numero ausente de "
        "zero e carrega a validade do dado: estoque expirado nao vira 'sem estoque'",
    ),
    _cap("list_categories", "SUPPORTED_DIRECTLY", "GET /v1/categories", False, "uso interno"),
    _cap("get_category", "UNSUPPORTED", None, False, "adaptador nao tem detalhe de categoria"),
    _cap("get_category_tree", "UNSUPPORTED", None, False, "adaptador nao monta arvore"),
    _cap("list_product_variants", "UNSUPPORTED", None, False, "variacao nao e recurso do adaptador"),
    _cap("get_product_variant", "UNSUPPORTED", None, False, "variacao nao e recurso do adaptador"),
    # --- clientes ---------------------------------------------------------
    _cap(
        "search_customer",
        "UNSUPPORTED",
        None,
        False,
        "listagem so aceita alterado_apos; buscar por CPF/CNPJ/e-mail exigiria "
        "baixar a base inteira a cada conversa (MERCOS_ADAPTOR_GAP)",
    ),
    _cap(
        "get_customer",
        "SUPPORTED_VIA_COMPOSITION",
        "GET /v1/customers/{mercos_id}",
        False,
        "endpoint existe; mapeamento de campos desconhecido (MERCOS_ADAPTOR_GAP)",
    ),
    _cap(
        "lookup_customer_by_document",
        "SUPPORTED_LOCAL",
        "GET /v1/customers?alterado_apos= (sync) -> ai_mercos_customer_document_index",
        False,
        "indice local de HMAC dos documentos, alimentado por sync incremental; "
        "somente um sync completo e recente permite distinguir NOT_FOUND de falha",
    ),
    _cap(
        "create_customer",
        "SUPPORTED_DIRECTLY",
        "POST /v1/customers",
        False,
        "fluxo determinístico; exige confirmação explícita e porta de mutação ativa",
    ),
    # --- pedidos ----------------------------------------------------------
    _cap(
        "list_orders",
        "UNSUPPORTED",
        None,
        False,
        "GET /v1/orders e incremental global, sem filtro por cliente: nao "
        "responde 'os pedidos DESTE cliente'",
    ),
    _cap(
        "get_order",
        "SUPPORTED_VIA_COMPOSITION",
        "GET /v1/orders/{mercos_id}",
        False,
        "endpoint existe e preserva itens (v2); mapeamento de campos "
        "desconhecido (MERCOS_ADAPTOR_GAP)",
    ),
    _cap("get_order_complete", "UNSUPPORTED", None, False, "sem mapeamento de campos"),
    _cap(
        "get_order_payment",
        "UNSUPPORTED",
        None,
        False,
        "a Mercos nao devolve link de pagamento equivalente ao fluxo anterior; "
        "inventar link seria o pior tipo de alucinacao",
    ),
    _cap(
        "create_order",
        "UNSUPPORTED",
        "POST /v1/orders",
        False,
        "endpoint existe, mas o payload exigido pela Mercos v2 nao esta "
        "documentado no adaptador; enviar campo inventado cria pedido errado",
    ),
    # --- carrinho: nao existe na Mercos -----------------------------------
    _cap("create_cart", "UNSUPPORTED", None, False, "a Mercos nao tem carrinho"),
    _cap("get_cart", "UNSUPPORTED", None, False, "a Mercos nao tem carrinho"),
    _cap("get_cart_complete", "UNSUPPORTED", None, False, "a Mercos nao tem carrinho"),
    _cap("set_cart_item_quantity", "UNSUPPORTED", None, False, "a Mercos nao tem carrinho"),
    _cap("delete_cart", "UNSUPPORTED", None, False, "a Mercos nao tem carrinho"),
    # --- cupons: nao existem ----------------------------------------------
    _cap("list_coupons", "UNSUPPORTED", None, False, "adaptador nao expoe cupom"),
    _cap("get_coupon", "UNSUPPORTED", None, False, "adaptador nao expoe cupom"),
    # --- frete: transportadora nao e cotacao ------------------------------
    _cap(
        "quote_shipping",
        "UNSUPPORTED",
        None,
        False,
        "/v1/carriers lista transportadoras; transportadora nao e cotacao",
    ),
    _cap(
        "list_shipping_methods",
        "UNSUPPORTED",
        None,
        False,
        "/v1/carriers lista transportadoras; transportadora nao e metodo de envio",
    ),
    # --- pagamento ---------------------------------------------------------
    _cap(
        "get_payment_options",
        "UNSUPPORTED",
        None,
        False,
        "/v1/payment-conditions sao condicoes comerciais B2B, semantica "
        "diferente das opcoes de pagamento de checkout; nao ha equivalencia",
    ),
)

CAPABILITY_BY_NAME: dict[str, CapabilitySupport] = {
    item.capability: item for item in CAPABILITY_MATRIX
}

#: Capacidades que este provider consegue executar de fato.
SUPPORTED_CAPABILITIES: frozenset[str] = frozenset(
    item.capability for item in CAPABILITY_MATRIX if item.supported
)

#: Subconjunto que pode ser oferecido como tool ao modelo.
LLM_EXPOSED_CAPABILITIES: frozenset[str] = frozenset(
    item.capability for item in CAPABILITY_MATRIX if item.exposed_to_llm
)


class MercosCommerceProvider:
    """Fonte comercial oficial via MercosAdaptor."""

    name = PROVIDER_NAME

    def __init__(
        self,
        client: MercosAdaptorClient,
        *,
        index: Any | None = None,
        tenant_id: str,
        sync_state: Any | None = None,
        customer_index: Any | None = None,
    ) -> None:
        """`tenant_id` e obrigatorio: fonte unica de verdade e o Settings.

        Qualquer default aqui seria uma segunda fonte de verdade — e no dia em
        que as duas divergissem, o catalogo seria gravado numa particao e lido
        de outra, devolvendo busca vazia sem erro nenhum.
        """
        if not str(tenant_id or "").strip():
            raise ValueError("tenant_id do dominio comercial e obrigatorio")
        self._client = client
        self._index = index
        self._tenant_id = str(tenant_id).strip()
        self._sync_state = sync_state
        self._customer_index = customer_index

    @property
    def sync_ready(self) -> bool:
        """Houve um sync de produtos concluido com sucesso?

        Configurado NAO e pronto. Sem esta checagem, ligar as envs exporia
        `search_products` sobre um indice nunca sincronizado: a busca voltaria
        vazia e o agente leria isso como "nao temos esse produto" — negativa
        comercial falsa, indistinguivel de um fato para o cliente.
        """
        if self._sync_state is None:
            return False
        try:
            return bool(self._sync_state.read(PROVIDER_NAME, "products").ready)
        except Exception:  # noqa: BLE001 - readiness nunca derruba o turno
            return False

    @property
    def available(self) -> bool:
        """Posso responder fato comercial ao modelo?

        Tres condicoes, todas necessarias: capacidade exposta, indice local
        presente e sync de produtos concluido. Faltando qualquer uma, o modelo
        nao recebe tool comercial alguma.
        """
        return (
            bool(LLM_EXPOSED_CAPABILITIES)
            and self._index is not None
            and self.sync_ready
        )

    @property
    def llm_capabilities(self) -> frozenset[str]:
        """Capacidades oferecidas ao modelo — vazio enquanto nao houver sync."""
        return LLM_EXPOSED_CAPABILITIES if self.available else frozenset()

    @property
    def supported_capabilities(self) -> frozenset[str]:
        return SUPPORTED_CAPABILITIES

    @property
    def runtime_capabilities(self) -> frozenset[str]:
        """Capabilities usable by deterministic flows in this environment."""
        capabilities = set(self.llm_capabilities)
        if self._customer_index is not None and self._customer_index.ready:
            capabilities.add("lookup_customer_by_document")
        if self._client.customer_mutations_enabled and "lookup_customer_by_document" in capabilities:
            capabilities.add("create_customer")
        return frozenset(capabilities)

    def sync_health(self) -> dict[str, Any]:
        """Projecao de prontidao para o /health. So booleanos e um timestamp."""
        base = {"commerce_adaptor_configured": True}
        if self._sync_state is None:
            return {
                **base,
                "mercos_product_sync_ready": False,
                "mercos_product_sync_last_success_at": None,
            }
        try:
            state = self._sync_state.read(PROVIDER_NAME, "products")
        except Exception:  # noqa: BLE001 - health nunca derruba o servico
            return {
                **base,
                "mercos_product_sync_ready": False,
                "mercos_product_sync_last_success_at": None,
            }
        return {
            **base,
            "mercos_product_sync_ready": state.ready,
            "mercos_product_sync_last_success_at": (
                state.last_success_at.isoformat() if state.last_success_at else None
            ),
            "mercos_sync_state_table_missing": state.missing_table,
            "mercos_sync_last_error_code": state.last_error_code,
        }

    async def run_product_sync(self) -> dict[str, Any]:
        """Ciclo de sync do catalogo. Entrypoint operacional do provider."""
        from .sync_runner import run_product_sync

        return await run_product_sync()

    async def run_product_full_refresh(self) -> dict[str, Any]:
        """Reconstroi o indice inteiro sem mover a posicao do incremental."""
        from .sync_runner import run_product_full_refresh

        return await run_product_full_refresh()

    async def execute(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Despacha por capacidade. Falha fechada e explicita."""
        support = CAPABILITY_BY_NAME.get(capability)
        if support is None or not support.supported:
            raise CommerceUnavailableError(capability)

        handler = getattr(self, f"_do_{capability}", None)
        if handler is None:
            raise CommerceUnavailableError(capability)
        try:
            return await handler(dict(arguments or {}))
        except MercosAdaptorError as exc:
            return {
                "ok": False,
                "error": "commerce_provider_error",
                "code": exc.code,
                "status_code": exc.status_code,
                "retry_after": exc.retry_after,
                "capability": capability,
            }

    # --- handlers ----------------------------------------------------------

    async def _do_search_products(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .catalog_search import search_products

        return search_products(
            self._index, tenant_id=self._tenant_id, arguments=arguments
        )

    async def _do_get_product(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .catalog_search import get_product

        product_id = arguments.get("product_id")
        if not product_id:
            return {"ok": False, "error": "missing_argument", "argument": "product_id"}
        return get_product(self._index, tenant_id=self._tenant_id, product_id=product_id)

    async def _do_check_inventory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .catalog_search import check_inventory

        product_id = arguments.get("product_id")
        if not product_id:
            return {"ok": False, "error": "missing_argument", "argument": "product_id"}
        return check_inventory(self._index, tenant_id=self._tenant_id, product_id=product_id)

    async def _do_create_customer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Exactly one POST per tenant + document, decided under the document lock.

        The caller's earlier lookup is only UX: the decision to create is the
        re-lookup done by ``claim_creation`` inside the per-document lock. A
        concurrent confirmation for the same document (another conversation or
        worker) finds the claim and never POSTs.
        """
        from .client import MercosAdaptorError, MercosMutationDisabled

        required = ("tipo", "razao_social", "cnpj")
        missing = [field for field in required if not arguments.get(field)]
        if missing:
            return {"ok": False, "error": "missing_argument", "arguments": missing}
        if not self._client.customer_mutations_enabled:
            return {"ok": False, "error": "mutation_disabled", "code": "mutation_disabled"}
        index = self._customer_index
        if index is None:
            return {"ok": False, "error": "creation_blocked", "code": "lookup_index_not_ready",
                    "lookup_status": "INDEX_NOT_READY"}
        document = str(arguments["cnpj"])
        claim = index.claim_creation(document)
        if not claim.claimed:
            lookup = claim.result
            if lookup.status.value == "FOUND" and lookup.customer_id:
                return {"ok": True, "customer_id": lookup.customer_id, "linked_existing": True}
            return {"ok": False, "error": "creation_blocked",
                    "code": f"lookup_{lookup.status.value.lower()}", "lookup_status": lookup.status.value}

        def _finish(outcome: str, customer_id: str | None = None) -> None:
            try:
                index.finish_creation(document, outcome=outcome, customer_id=customer_id)
            except Exception:  # noqa: BLE001 - claim stays 'creating' and keeps blocking
                pass

        try:
            raw = await self._client.create_customer(arguments)
        except MercosMutationDisabled:
            _finish("released")
            return {"ok": False, "error": "mutation_disabled", "code": "mutation_disabled"}
        except MercosAdaptorError as exc:
            # Definitive rejections mean nothing was created; anything else
            # (transport, 5xx, unreadable body) may have created the customer.
            if exc.code in {"bad_request", "unauthorized", "not_found", "rate_limited"}:
                _finish("released")
            else:
                _finish("unknown")
            if exc.status_code in {400, 409, 422}:
                from .customer_validation import parse_customer_validation, is_duplicate_document_error

                if is_duplicate_document_error(exc.details, str(exc)):
                    try:
                        await self.run_customer_sync()
                    except Exception:  # noqa: BLE001 - unresolved duplicate goes to handoff
                        pass
                    lookup = index.lookup_customer_by_document(document)
                    if lookup.status.value == "FOUND" and lookup.customer_id:
                        return {"ok": True, "customer_id": lookup.customer_id, "linked_existing": True}
                    return {"ok": False, "code": "duplicate_document"}
                if exc.status_code == 422:
                    fields = parse_customer_validation(exc.details, str(exc))
                    return {"ok": False, "code": "customer_validation", "fields": list(fields)}
            raise
        except Exception:
            _finish("unknown")
            return {"ok": False, "error": "commerce_provider_error", "code": "mutation_state_unknown"}
        payload = raw if isinstance(raw, dict) else {}
        nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        customer_id = (
            payload.get("customer_id") or payload.get("id")
            or payload.get("mercos_id") or nested.get("id")
        )
        if customer_id is None:
            _finish("created_pending_sync")
            try:
                await self.run_customer_sync()
                lookup = index.lookup_customer_by_document(document)
                if lookup.status.value == "FOUND" and lookup.customer_id:
                    _finish("created", lookup.customer_id)
                    return {"ok": True, "customer_id": lookup.customer_id}
            except Exception:  # noqa: BLE001 - 2xx remains successful, claim blocks a second POST
                pass
            return {"ok": True, "status": "CREATED_PENDING_SYNC"}
        _finish("created", str(customer_id))
        return {"ok": True, "customer_id": str(customer_id)}

    async def _do_lookup_customer_by_document(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._customer_index is None:
            from .customer_index import CustomerLookupResult, CustomerLookupStatus

            return CustomerLookupResult(CustomerLookupStatus.PROVIDER_UNAVAILABLE).as_tool_result()
        return self._customer_index.lookup_customer_by_document(
            str(arguments.get("document") or "")
        ).as_tool_result()

    async def run_customer_sync(self) -> dict[str, Any]:
        from .customer_index import run_customer_sync

        return await run_customer_sync()

    async def list_payment_conditions(self) -> list[dict[str, Any]]:
        """Condicoes de pagamento cadastradas na fonte comercial.

        Capacidade PUBLICA do provider, e nao uma tool do modelo. O fluxo de
        revisao chega ate a fonte por aqui em vez de alcancar `_client` por
        dentro: quem consome o provider deve enxergar capacidade, nao fiacao —
        e isso e o que sobrevive a uma troca de fornecedor.

        A regra de selecao (uma ativa seleciona; varias perguntam; nenhuma
        bloqueia) vive fora, em `commerce.payment_conditions`, para poder ser
        testada sem rede.
        """
        page = await self._client.list_resource("payment-conditions")
        return list(page.data or [])

    async def _do_list_categories(self, arguments: dict[str, Any]) -> dict[str, Any]:
        page = await self._client.list_resource(
            "categories", changed_after=arguments.get("changed_after")
        )
        return {
            "ok": True,
            "count": page.count,
            "next_cursor": page.next_cursor,
            "categories": [
                record.identity()
                for record in _identities("categories", page.data)
            ],
        }


def _identities(resource: str, rows: list[dict[str, Any]]):
    from .normalizer import normalize_page

    return normalize_page(resource, rows)


def build_mercos_provider(settings: Any, *, index: Any | None = None) -> MercosCommerceProvider:
    """Constroi o provider a partir das duas unicas envs permitidas.

    O indice local so e ligado quando ha banco configurado: as capacidades
    expostas leem dele, e sem banco o provider fica indisponivel em vez de
    oferecer tools que falhariam.
    """
    client = MercosAdaptorClient(
        base_url=settings.mercos_adaptor_url,
        api_key=settings.mercos_adaptor_api_key,
        timeout_seconds=getattr(settings, "mercos_adaptor_timeout_seconds", 90.0),
        customer_mutations_enabled=bool(
            getattr(settings, "mercos_customer_mutations_enabled", False)
        ),
    )
    # Tenant COMERCIAL, nunca o da persona. Fonte unica: Settings — sem
    # fallback literal aqui, para nao existir uma segunda verdade.
    tenant_id = settings.commerce_tenant_id
    resolved_index = index
    sync_state = None
    customer_index = None
    if getattr(settings, "database_url", ""):
        if resolved_index is None:
            from .catalog_index_reader import CatalogIndexProductReader

            resolved_index = CatalogIndexProductReader()
        from .sync_state import DatabaseSyncStateStore

        sync_state = DatabaseSyncStateStore(tenant_id=tenant_id)
        from .customer_index import CustomerDocumentIndex

        customer_index = CustomerDocumentIndex(
            tenant_id=tenant_id,
            hmac_key=getattr(settings, "customer_document_hmac_key", ""),
            state_store=sync_state,
        )
    return MercosCommerceProvider(
        client,
        index=resolved_index,
        tenant_id=tenant_id,
        sync_state=sync_state,
        customer_index=customer_index,
    )
