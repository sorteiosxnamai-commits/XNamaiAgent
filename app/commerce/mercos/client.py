"""HTTP com o MercosAdaptor. Responsabilidade unica: falar com o adaptador.

O XNamai NUNCA fala com `app.mercos.com`. Toda chamada sai daqui para o
MercosAdaptor, autenticada por uma chave interna (`X-API-Key`). Os tokens da
Mercos (ApplicationToken/CompanyToken) vivem SOMENTE no adaptador.

Contrato implementado, lido em sorteiosxnamai-commits/MercosAdaptor @38f0bf4:

    GET  /health
    GET  /v1/resources
    GET  /v1/{resource}?alterado_apos=<cursor>
    GET  /v1/{customers|products|orders}/{mercos_id}
    POST /v1/customers            PUT /v1/customers/{mercos_id}
    POST /v1/orders               PUT /v1/orders/{mercos_id}
    POST /v1/titles               PUT /v1/titles/{mercos_id}

Envelope de listagem::

    {"resource": ..., "count": ..., "pageCursor": ..., "nextCursor": ..., "data": [...]}

`nextCursor` so vem preenchido quando existe proxima pagina; o consumidor so
pode persistir o cursor DEPOIS de gravar toda a pagina com sucesso.

Retries — cuidado deliberado
----------------------------
O adaptador JA implementa retry contra a Mercos (backoff, 429 com
`tempo_ate_permitir_novamente`, pausa entre paginas e um lock global que
serializa as chamadas). Repetir aqui multiplicaria a carga e o tempo de espera.
Por isso:

* 429 NAO e reexecutado: vira erro estruturado com `retry_after` para quem
  chamou decidir;
* erro de transporte (rede) tem retry curto, so para o salto XNamai -> adaptor;
* MUTACAO (POST/PUT) nunca e repetida: sem idempotencia garantida, repetir um
  `POST /v1/orders` criaria pedido duplicado.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

#: Nomes de chave que nunca podem aparecer em log ou em detalhe de erro.
_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "xapikey",
        "authorization",
        "applicationtoken",
        "companytoken",
        "token",
        "password",
        "secret",
    }
)

#: Recursos de listagem expostos pelo adaptador (`app/resources.py` de la).
ADAPTOR_LIST_RESOURCES: frozenset[str] = frozenset(
    {
        "customers",
        "products",
        "orders",
        "price-tables",
        "payment-conditions",
        "carriers",
        "commercial-policies",
        "categories",
        "segments",
        "order-types",
        "product-prices",
        "users",
    }
)

#: Recursos com consulta individual. Os demais devolvem 404 no adaptador.
ADAPTOR_DETAIL_RESOURCES: frozenset[str] = frozenset({"customers", "products", "orders"})


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "").replace("_", "")


def sanitize(value: Any) -> Any:
    """Remove segredo de qualquer estrutura antes de logar ou anexar a erro."""
    if isinstance(value, dict):
        return {
            key: sanitize(item)
            for key, item in value.items()
            if _normalize_key(str(key)) not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


class MercosAdaptorError(RuntimeError):
    """Falha estruturada ao falar com o adaptador. Nunca carrega segredo."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "adaptor_error",
        retry_after: float | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        self.details = sanitize(details)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"MercosAdaptorError(code={self.code!r}, status_code={self.status_code!r}, "
            f"retry_after={self.retry_after!r})"
        )


@dataclass(frozen=True)
class AdaptorPage:
    """Uma pagina do envelope de listagem do adaptador."""

    resource: str
    count: int
    data: list[dict[str, Any]] = field(default_factory=list)
    page_cursor: str | None = None
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        """Existe proxima pagina? So quando o adaptador devolveu `nextCursor`."""
        return bool(self.next_cursor)


class MercosAdaptorClient:
    """Cliente async do MercosAdaptor."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_transport_retries: int = 2,
    ) -> None:
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = (api_key or "").strip()
        self._timeout = timeout_seconds
        self._transport = transport
        self._max_transport_retries = max(1, int(max_transport_retries))

    # --- infraestrutura ----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._api_key,
            "Accept": "application/json",
            "User-Agent": "XNamaiAgent/commerce",
        }

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    def _retry_after_from(response: httpx.Response) -> float | None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict):
            details = payload.get("details")
            raw = None
            if isinstance(details, dict):
                raw = details.get("tempo_ate_permitir_novamente")
            if raw is None:
                raw = payload.get("tempo_ate_permitir_novamente")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None
        return None

    def _raise_for_status(self, response: httpx.Response) -> None:
        if not response.is_error:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": response.text[:500]}
        message = "adaptador comercial rejeitou a requisicao"
        details = None
        if isinstance(payload, dict):
            message = str(payload.get("error") or message)
            details = payload.get("details")
        if response.status_code == 429:
            raise MercosAdaptorError(
                "limite de requisicoes do adaptador comercial",
                status_code=429,
                code="rate_limited",
                retry_after=self._retry_after_from(response),
                details=details,
            )
        if response.status_code in (401, 403):
            code = "unauthorized"
        elif response.status_code == 404:
            code = "not_found"
        elif 400 <= response.status_code < 500:
            code = "bad_request"
        else:
            code = "adaptor_unavailable"
        raise MercosAdaptorError(
            message, status_code=response.status_code, code=code, details=details
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        allow_retry: bool = True,
    ) -> Any:
        """Executa uma chamada. `allow_retry=False` para mutacoes."""
        attempts = self._max_transport_retries if allow_retry else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    headers=self._headers(),
                    timeout=self._timeout,
                    transport=self._transport,
                ) as client:
                    response = await client.request(
                        method, self._url(path), params=params, json=json
                    )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(2**attempt, 4))
                    continue
                raise MercosAdaptorError(
                    "adaptador comercial inacessivel",
                    code="transport_error",
                    details={"error_type": type(exc).__name__},
                ) from exc
            self._raise_for_status(response)
            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise MercosAdaptorError(
                    "adaptador comercial devolveu resposta ilegivel",
                    status_code=response.status_code,
                    code="invalid_response",
                ) from exc
        raise MercosAdaptorError(  # pragma: no cover - defensivo
            "adaptador comercial inacessivel",
            code="transport_error",
            details={"error_type": type(last_error).__name__ if last_error else None},
        )

    # --- leitura -----------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        payload = await self._request("GET", "/health")
        return payload if isinstance(payload, dict) else {}

    async def resources(self) -> list[str]:
        payload = await self._request("GET", "/v1/resources")
        if isinstance(payload, dict):
            found = payload.get("resources")
            if isinstance(found, list):
                return [str(item) for item in found]
        return []

    async def list_resource(
        self, resource: str, *, changed_after: str | None = None
    ) -> AdaptorPage:
        """Uma pagina de `GET /v1/{resource}`.

        O adaptador aceita SOMENTE `alterado_apos` como filtro — nao ha busca
        textual nem filtro por documento. Quem precisar de busca usa o indice
        local do XNamai.
        """
        params = {"alterado_apos": changed_after} if changed_after else None
        payload = await self._request("GET", f"/v1/{resource}", params=params)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise MercosAdaptorError(
                "envelope de listagem invalido",
                code="invalid_response",
                details={"resource": resource},
            )
        return AdaptorPage(
            resource=str(payload.get("resource") or resource),
            count=int(payload.get("count") or 0),
            data=[row for row in payload["data"] if isinstance(row, dict)],
            page_cursor=payload.get("pageCursor"),
            next_cursor=payload.get("nextCursor"),
        )

    async def get_detail(self, resource: str, mercos_id: str) -> dict[str, Any]:
        """`GET /v1/{resource}/{mercos_id}`.

        Atencao: o proprio adaptador documenta que o GET por id da Mercos pode
        responder 401/403 em producao (so liberado em sandbox). Nesse caso vem
        `code="unauthorized"` e o chamador deve tratar como fato indisponivel —
        nunca inventar o dado.
        """
        payload = await self._request("GET", f"/v1/{resource}/{mercos_id}")
        if not isinstance(payload, dict):
            raise MercosAdaptorError(
                "detalhe invalido",
                code="invalid_response",
                details={"resource": resource},
            )
        return payload

    # --- mutacoes: nunca repetidas automaticamente -------------------------
    #
    # Um timeout DEPOIS do envio e ambiguo: o pedido pode ter sido criado. Nesse
    # caso o erro carrega ``mutation_state="unknown"`` para que ninguem — nem
    # codigo, nem operador — trate a falha como "nao aconteceu" e reenvie,
    # criando pedido duplicado no ERP do cliente.

    async def _mutate(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        try:
            return await self._request(method, path, json=payload, allow_retry=False)
        except MercosAdaptorError as exc:
            if exc.code == "transport_error" or exc.status_code in (502, 503, 504):
                raise MercosAdaptorError(
                    "resultado da mutacao desconhecido",
                    status_code=exc.status_code,
                    code="mutation_state_unknown",
                    details={"mutation_state": "unknown", "original_code": exc.code},
                ) from exc
            raise

    async def create_customer(self, payload: dict[str, Any]) -> Any:
        return await self._mutate("POST", "/v1/customers", payload)

    async def update_customer(self, mercos_id: str, payload: dict[str, Any]) -> Any:
        return await self._mutate("PUT", f"/v1/customers/{mercos_id}", payload)

    async def create_order(self, payload: dict[str, Any]) -> Any:
        return await self._mutate("POST", "/v1/orders", payload)

    async def update_order(self, mercos_id: str, payload: dict[str, Any]) -> Any:
        return await self._mutate("PUT", f"/v1/orders/{mercos_id}", payload)
