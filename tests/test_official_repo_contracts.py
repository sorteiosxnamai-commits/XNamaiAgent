"""Consumer contracts against the sibling official repositories.

Two layers:

* fixtures in ``tests/fixtures/contracts`` were produced by running the REAL
  routes of MercosAdaptor and xnamai-club-backend; the agent is tested against
  them on every run (CI included);
* ``cross_repo`` tests regenerate/inspect those payloads from the sibling
  checkouts and fail on drift. Without the siblings they are SKIPPED.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.club_provider import ClubProvider
from app.commerce.mercos.client import MercosAdaptorClient
from app.commerce.mercos.customer_index import document_digest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contracts"
ADAPTOR = ROOT / "MercosAdaptor"
CLUB = ROOT / "xnamai-club-backend"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _sibling(path: Path):
    return pytest.mark.skipif(not path.is_dir(), reason=f"sibling repo {path.name} not checked out")


# --- agent vs recorded payloads (always runs) -------------------------------------------


@pytest.mark.asyncio
async def test_agent_reads_the_real_adaptor_customer_page():
    page = _fixture("mercos_adaptor.json")["customers_page"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/v1/customers"
        assert list(request.url.params.keys()) == ["alterado_apos"]
        assert request.headers["X-API-Key"] == "internal"
        return httpx.Response(200, json=page)

    client = MercosAdaptorClient(base_url="https://adaptor.example", api_key="internal",
                                 transport=httpx.MockTransport(handler))
    result = await client.list_resource("customers", changed_after="2026-08-01T00:00:00")
    assert result.data[0]["cnpj"] == "52998224725"
    assert result.data[0]["excluido"] is False
    assert result.next_cursor is None and result.page_cursor == "2026-09-01T00:00:00"


@pytest.mark.asyncio
async def test_agent_reads_the_created_id_the_real_adaptor_returns():
    from app.commerce.mercos.customer_index import CreationClaim, CustomerLookupResult, CustomerLookupStatus
    from app.commerce.mercos.provider import MercosCommerceProvider

    created = _fixture("mercos_adaptor.json")["customer_created"]

    class ClaimingIndex:
        outcomes: list = []

        def claim_creation(self, document):
            return CreationClaim(True, CustomerLookupResult(CustomerLookupStatus.NOT_FOUND))

        def finish_creation(self, document, *, outcome, customer_id=None):
            self.outcomes.append((outcome, customer_id))

    client = MercosAdaptorClient(base_url="https://adaptor.example", api_key="internal",
                                 customer_mutations_enabled=True,
                                 transport=httpx.MockTransport(lambda _: httpx.Response(200, json=created)))
    index = ClaimingIndex()
    provider = MercosCommerceProvider(client, tenant_id="xnamai", customer_index=index)
    result = await provider.execute("create_customer", {
        "tipo": "F", "razao_social": "Cliente Sintetico", "nome_fantasia": "Cliente Sintetico",
        "cnpj": "52998224725"})
    assert result == {"ok": True, "customer_id": "321"}
    assert index.outcomes == [("created", "321")]


@pytest.mark.asyncio
async def test_agent_reads_the_real_club_plans_payload():
    recorded = _fixture("club_plans.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/api/plans"
        assert "authorization" not in request.headers  # rota publica: nenhuma credencial
        return httpx.Response(recorded["status"], json=recorded["body"])

    plans = await ClubProvider("https://club-api.example", transport=httpx.MockTransport(handler)).get_public_plans()
    assert plans and plans[0].name == "Plano Club" and plans[0].monthly_price_cents == 14997


@pytest.mark.asyncio
async def test_club_invalid_plan_payload_is_unavailable():
    provider = ClubProvider("https://club-api.example", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"plans": []})
    ))
    assert await provider.get_public_plans() is None


def test_document_index_digest_is_keyed_and_normalized():
    # Lookup, baseline, rotation, AMBIGUOUS and the per-document lock are proven
    # against a real PostgreSQL in tests/test_customer_document_index_postgres.py.
    assert document_digest("529.982.247-25", "k" * 32) == document_digest("52998224725", "k" * 32)
    assert document_digest("52998224725", "k" * 32) != document_digest("52998224725", "x" * 32)
    assert "52998224725" not in document_digest("52998224725", "k" * 32)


# --- drift against the sibling checkouts --------------------------------------------------

_ADAPTOR_SCRIPT = r'''
import json
import httpx
from fastapi.testclient import TestClient
from app.main import app
from app.client import MercosClient
from app.config import Settings
from app.security import require_api_key

settings = Settings(mercos_application_token="app", mercos_company_token="company",
                    mercos_adaptor_api_key="internal", mercos_default_retry_seconds=0,
                    mercos_page_pause_seconds=0)

def upstream(request):
    if request.method == "GET":
        assert request.url.path == "/api/v1/clientes"
        return httpx.Response(200, json=[{"id": 42, "razao_social": "Cliente Sintetico", "tipo": "F",
                                          "cnpj": "52998224725", "excluido": False,
                                          "ultima_alteracao": "2026-09-01T00:00:00"}])
    assert request.method == "POST" and request.url.path == "/api/v1/clientes"
    return httpx.Response(201, headers={"MeusPedidosID": "321"})

original = MercosClient.__init__
MercosClient.__init__ = lambda self, s=None, t=None: original(self, settings, httpx.MockTransport(upstream))
app.dependency_overrides[require_api_key] = lambda: None
client = TestClient(app)
page = client.get("/v1/customers", params={"alterado_apos": "2026-08-01T00:00:00"})
created = client.post("/v1/customers", json={"tipo": "F", "razao_social": "Cliente Sintetico"})
assert page.status_code == 200 and created.status_code == 200
print(json.dumps({"customers_page": page.json(), "customer_created": created.json()}))
'''


@pytest.mark.cross_repo
@_sibling(ADAPTOR)
def test_adaptor_fixture_matches_the_real_adaptor():
    env = {**os.environ, "PYTHONPATH": str(ADAPTOR)}
    process = subprocess.run([sys.executable, "-c", _ADAPTOR_SCRIPT], cwd=ADAPTOR,
                             capture_output=True, text=True, env=env)
    if process.returncode != 0 and "ModuleNotFoundError" in process.stderr:
        pytest.skip("MercosAdaptor dependencies not installed in this interpreter")
    assert process.returncode == 0, process.stderr[-2000:]
    live = json.loads(process.stdout.strip().splitlines()[-1])
    recorded = _fixture("mercos_adaptor.json")
    assert live["customers_page"] == recorded["customers_page"]
    assert live["customer_created"] == recorded["customer_created"]


@pytest.mark.cross_repo
@_sibling(ADAPTOR)
def test_adaptor_has_no_document_lookup_route():
    main = (ADAPTOR / "app" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/v1/{resource}",' in main and 'alias="alterado_apos"' in main
    assert "by-document" not in main and "cnpj" not in main  # nada de filtro por documento


@pytest.mark.cross_repo
@_sibling(CLUB)
def test_club_plan_fixture_matches_the_plan_entity_and_routes_are_protected():
    import re

    entity = (CLUB / "src" / "entities" / "Plan.ts").read_text(encoding="utf-8")
    columns = set(re.findall(r"^\s+(\w+)!:", entity, flags=re.MULTILINE)) - {"subscriptions"}
    assert set(_fixture("club_plans.json")["body"][0]) == columns

    server = (CLUB / "src" / "server.ts").read_text(encoding="utf-8")
    assert "api.use('/plans', plansRouter)" in server and "app.use('/api', api)" in server
    assert "app.use(api)" not in server  # rotas so existem sob /api
    atendimento = (CLUB / "src" / "routes" / "atendimento.ts").read_text(encoding="utf-8")
    assert "atendimentoRouter.use(requireAuth, requireRole(" in atendimento
    subscriptions = (CLUB / "src" / "routes" / "subscriptions.ts").read_text(encoding="utf-8")
    assert "subscriptionsRouter.use(requireAuth)" in subscriptions
