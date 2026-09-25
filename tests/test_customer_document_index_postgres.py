"""Customer document index against a REAL PostgreSQL (migrations 023 + 025).

A mocked cursor cannot prove the advisory lock, the claim's primary key or the
baseline semantics. Runs only with a disposable database:
``TEST_DATABASE_URL=postgresql://... pytest -m integration``. Without it the
module is SKIPPED â€” never counted as passed.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.commerce.mercos.client import AdaptorPage, MercosAdaptorError
from app.commerce.mercos.customer_index import (
    CustomerDocumentIndex,
    CustomerLookupStatus,
    run_customer_sync,
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
REPO = pathlib.Path(__file__).resolve().parents[1]
KEY_A = "a" * 40
KEY_B = "b" * 40
CPF = "52998224725"
OTHER_CPF = "11144477735"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set (needs a disposable PostgreSQL)"),
]


@pytest.fixture
def tenant(monkeypatch):
    from app import db
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    with db.get_conn() as conn, conn.cursor() as cur:
        for name in ("023_mercos_sync_state.sql", "025_mercos_customer_document_index.sql",
                     "027_mercos_customer_creation_pending_sync.sql"):
            cur.execute((REPO / "sql" / name).read_text(encoding="utf-8"))
    tenant_id = f"it-{uuid4().hex[:10]}"
    try:
        yield tenant_id
    finally:
        with db.get_conn() as conn, conn.cursor() as cur:
            for table in ("ai_mercos_customer_document_index", "ai_mercos_customer_index_config",
                          "ai_mercos_customer_creation_claim", "ai_commerce_sync_state"):
                cur.execute(f"DELETE FROM public.{table} WHERE tenant_id = %s", (tenant_id,))
        get_settings.cache_clear()


class PagedClient:
    """Adaptor list envelope (`data`, `pageCursor`, `nextCursor`) one page at a time."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = list(pages)
        self.requested: list[str | None] = []

    async def list_resource(self, resource, *, changed_after=None):
        assert resource == "customers"
        self.requested.append(changed_after)
        data = self.pages.pop(0) if self.pages else []
        cursor = data[-1]["ultima_alteracao"] if data else changed_after
        return AdaptorPage(resource=resource, count=len(data), data=data, page_cursor=cursor,
                           next_cursor=cursor if self.pages else None)


def _row(customer_id: int, document: str, when: str, **extra) -> dict:
    return {"id": customer_id, "cnpj": document, "ultima_alteracao": when, **extra}


def _settings(tenant_id: str, key: str = KEY_A):
    return SimpleNamespace(mercos_adaptor_configured=True, database_url=TEST_DATABASE_URL,
                           customer_document_hmac_key=key, commerce_tenant_id=tenant_id)


def _index(tenant_id: str, key: str = KEY_A) -> CustomerDocumentIndex:
    return CustomerDocumentIndex(tenant_id=tenant_id, hmac_key=key)


async def _sync(tenant_id: str, pages, *, key: str = KEY_A, max_pages: int = 50):
    client = PagedClient(pages)
    result = await run_customer_sync(settings=_settings(tenant_id, key), client=client, max_pages=max_pages)
    return result, client


class FakeAdaptor:
    """Counts POSTs; can delay or fail them. Stands in for MercosAdaptorClient."""

    customer_mutations_enabled = True

    def __init__(self, *, delay: float = 0.0, error: MercosAdaptorError | None = None, customer_id: str = "700"):
        self.posts = 0
        self.delay = delay
        self.error = error
        self.customer_id = customer_id
        self._lock = threading.Lock()

    async def create_customer(self, payload):
        with self._lock:
            self.posts += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return {"id": self.customer_id}


def _provider(tenant_id: str, adaptor: FakeAdaptor, key: str = KEY_A):
    from app.commerce.mercos.provider import MercosCommerceProvider

    return MercosCommerceProvider(adaptor, tenant_id=tenant_id, customer_index=_index(tenant_id, key))


def _payload(document: str = CPF) -> dict:
    return {"tipo": "F", "razao_social": "Cliente Teste", "nome_fantasia": "Cliente Teste", "cnpj": document}


# --- baseline / readiness ------------------------------------------------------------


def test_never_synced_index_is_not_ready(tenant):
    index = _index(tenant)
    assert index.not_ready_reason() == "never_synced"
    assert index.lookup_customer_by_document(CPF).status is CustomerLookupStatus.INDEX_NOT_READY


@pytest.mark.asyncio
async def test_partial_baseline_is_not_ready_and_completion_makes_it_usable(tenant):
    pages = [[_row(1, OTHER_CPF, "2026-09-01T00:00:00")], [_row(2, CPF, "2026-09-02T00:00:00")]]
    first, _ = await _sync(tenant, pages[:], max_pages=1)
    assert first["ok"] is False and first["stopped_reason"] == "page_budget"
    index = _index(tenant)
    assert index.not_ready_reason() in {"baseline_in_progress", "last_sync_failed"}
    assert index.lookup_customer_by_document("39053344705").status is CustomerLookupStatus.INDEX_NOT_READY

    second, client = await _sync(tenant, [pages[1]])
    assert second["ok"] is True
    assert client.requested[0] == "2026-09-01T00:00:00"  # continuou a MESMA varredura
    assert index.not_ready_reason() is None
    assert index.lookup_customer_by_document(CPF).customer_id == "2"
    assert index.lookup_customer_by_document("39053344705").status is CustomerLookupStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_a_leftover_incremental_cursor_is_not_a_baseline(tenant):
    """Um cursor antigo sem baseline registrado nao prova ausencia: recomeca do zero."""
    from app.commerce.mercos.sync_state import DatabaseSyncStateStore

    DatabaseSyncStateStore(tenant_id=tenant).set_cursor("mercos", "customers", "2026-09-10T00:00:00")
    result, client = await _sync(tenant, [[_row(1, CPF, "2026-09-01T00:00:00")]])
    assert result["ok"] is True and result["baseline_started"] is True
    assert client.requested[0] is None


@pytest.mark.asyncio
async def test_stale_baseline_is_not_ready(tenant):
    from app import db

    await _sync(tenant, [[_row(1, CPF, "2026-09-01T00:00:00")]])
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE public.ai_commerce_sync_state SET last_success_at = now() - interval '2 hours' "
                    "WHERE tenant_id = %s", (tenant,))
    index = _index(tenant)
    assert index.not_ready_reason() == "stale"
    assert index.lookup_customer_by_document("39053344705").status is CustomerLookupStatus.INDEX_NOT_READY


@pytest.mark.asyncio
async def test_index_stays_ready_after_an_incremental_sync_with_no_changes(tenant):
    """The scheduled incremental tick (empty page: nothing changed since the
    cursor) must not disturb an already-ready index."""
    await _sync(tenant, [[_row(1, CPF, "2026-09-01T00:00:00")]])
    index = _index(tenant)
    assert index.not_ready_reason() is None

    second, client = await _sync(tenant, [[]])
    assert second["ok"] is True
    assert client.requested[0] == "2026-09-01T00:00:00"
    assert index.not_ready_reason() is None
    assert index.lookup_customer_by_document(CPF).customer_id == "1"


@pytest.mark.asyncio
async def test_a_failed_incremental_sync_preserves_the_previous_ready_index(tenant):
    """A Mercos read failure on the scheduled tick must record the failure
    without erasing the index a prior successful sync already built."""
    await _sync(tenant, [[_row(1, CPF, "2026-09-01T00:00:00")]])
    index = _index(tenant)
    assert index.not_ready_reason() is None

    class _FailingClient:
        async def list_resource(self, resource, *, changed_after=None):
            raise MercosAdaptorError("adaptor unavailable", code="adaptor_unavailable")

    result = await run_customer_sync(
        settings=_settings(tenant), client=_FailingClient(), max_pages=1,
    )
    assert result["ok"] is False
    assert result["error_code"] == "adaptor_unavailable"
    # The previously built index is untouched: still ready, still finds the
    # same customer — a failed tick never deletes or rebuilds it.
    assert index.not_ready_reason() is None
    assert index.lookup_customer_by_document(CPF).customer_id == "1"


# --- key rotation --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_rotation_blocks_lookup_and_creation_until_rebuild(tenant):
    await _sync(tenant, [[_row(1, OTHER_CPF, "2026-09-01T00:00:00")]], key=KEY_A)
    rotated = _index(tenant, KEY_B)
    assert rotated.not_ready_reason() == "key_rotated_rebuild_required"
    assert rotated.lookup_customer_by_document(CPF).status is CustomerLookupStatus.INDEX_NOT_READY

    adaptor = FakeAdaptor()
    result = await _provider(tenant, adaptor, KEY_B).execute("create_customer", _payload())
    assert result["ok"] is False and result["lookup_status"] == "INDEX_NOT_READY"
    assert adaptor.posts == 0

    rebuilt, client = await _sync(tenant, [[_row(1, OTHER_CPF, "2026-09-01T00:00:00")]], key=KEY_B)
    assert rebuilt["ok"] is True and rebuilt["baseline_started"] is True and client.requested[0] is None
    assert rotated.lookup_customer_by_document(OTHER_CPF).status is CustomerLookupStatus.FOUND


# --- AMBIGUOUS / excluido --------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_duplicate_document_is_ambiguous_and_never_creates(tenant):
    await _sync(tenant, [[_row(100, CPF, "2026-09-01T00:00:00"), _row(200, CPF, "2026-09-02T00:00:00")]])
    index = _index(tenant)
    lookup = index.lookup_customer_by_document(CPF)
    assert lookup.status is CustomerLookupStatus.AMBIGUOUS and lookup.customer_id is None

    adaptor = FakeAdaptor()
    result = await _provider(tenant, adaptor).execute("create_customer", _payload())
    assert result == {"ok": False, "error": "creation_blocked", "code": "lookup_ambiguous",
                      "lookup_status": "AMBIGUOUS"}
    assert adaptor.posts == 0


@pytest.mark.asyncio
async def test_explicitly_deleted_customer_leaves_the_index(tenant):
    await _sync(tenant, [[_row(1, CPF, "2026-09-01T00:00:00")]])
    await _sync(tenant, [[_row(1, CPF, "2026-09-02T00:00:00", excluido=True)]])
    assert _index(tenant).lookup_customer_by_document(CPF).status is CustomerLookupStatus.NOT_FOUND


# --- one POST per document ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_conversations_confirming_the_same_document_post_once(tenant):
    await _sync(tenant, [[_row(1, OTHER_CPF, "2026-09-01T00:00:00")]])
    adaptor = FakeAdaptor(delay=0.4)
    results: dict[str, dict] = {}
    barrier = threading.Barrier(2)

    def confirm(name: str) -> None:
        provider = _provider(tenant, adaptor)  # worker proprio, conexao propria
        barrier.wait()
        results[name] = asyncio.run(provider.execute("create_customer", _payload()))

    threads = [threading.Thread(target=confirm, args=(name,)) for name in ("whatsapp-a", "whatsapp-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert adaptor.posts == 1
    created = [r for r in results.values() if r.get("ok") and not r.get("linked_existing")]
    blocked = [r for r in results.values() if not r.get("ok")]
    assert len(created) == 1 and created[0]["customer_id"] == "700"
    assert blocked and blocked[0]["lookup_status"] == "CREATION_PENDING"

    # Depois do POST (antes do proximo sync) o mesmo documento e FOUND pela claim.
    again = await _provider(tenant, adaptor).execute("create_customer", _payload())
    assert again == {"ok": True, "customer_id": "700", "linked_existing": True}
    assert adaptor.posts == 1


@pytest.mark.asyncio
async def test_registration_flow_in_a_second_conversation_links_instead_of_creating(tenant):
    from app.commerce_context import CommerceConversationState
    from app.customer_registration import PENDING_REGISTRATION_CONFIRMATION, handle_customer_registration_turn

    await _sync(tenant, [[_row(1, OTHER_CPF, "2026-09-01T00:00:00")]])
    adaptor = FakeAdaptor()
    provider = _provider(tenant, adaptor)
    draft = {"person_type": "F", "legal_name": "Cliente Teste", "trade_name": "Cliente Teste",
             "document": CPF, "email": "cliente@example.invalid", "phone": "11988887777"}

    async def conversation():
        state = CommerceConversationState(pending_action=PENDING_REGISTRATION_CONFIRMATION,
                                          customer_registration={"status": "review", "draft": dict(draft)})
        return await handle_customer_registration_turn("confirmo o cadastro", state=state, execute=provider.execute)

    first, second = await conversation(), await conversation()
    assert first.response_metadata["customer_registration_state"]["status"] == "created"
    assert second.response_metadata["customer_registration_state"]["status"] == "linked"
    assert second.response_metadata["customer_registration_state"]["customer_id"] == "700"
    assert adaptor.posts == 1


@pytest.mark.asyncio
async def test_unknown_outcome_keeps_blocking_and_blocks_a_key_rebuild(tenant):
    await _sync(tenant, [[_row(1, OTHER_CPF, "2026-09-01T00:00:00")]])
    unknown = MercosAdaptorError("resultado desconhecido", status_code=504, code="mutation_state_unknown")
    adaptor = FakeAdaptor(error=unknown)
    first = await _provider(tenant, adaptor).execute("create_customer", _payload())
    assert first["ok"] is False and first["code"] == "mutation_state_unknown"
    assert _index(tenant).lookup_customer_by_document(CPF).status is CustomerLookupStatus.CREATION_PENDING
    second = await _provider(tenant, FakeAdaptor()).execute("create_customer", _payload())
    assert second["lookup_status"] == "CREATION_PENDING"
    assert adaptor.posts == 1

    rebuild, _ = await _sync(tenant, [[]], key=KEY_B)
    assert rebuild == {"ok": False, "error": "unresolved_creation_claims_block_rebuild"}


@pytest.mark.asyncio
async def test_definitive_rejection_releases_the_document(tenant):
    await _sync(tenant, [[_row(1, OTHER_CPF, "2026-09-01T00:00:00")]])
    rejected = MercosAdaptorError("rejeitado", status_code=400, code="bad_request")
    first = await _provider(tenant, FakeAdaptor(error=rejected)).execute("create_customer", _payload())
    assert first["ok"] is False and first["code"] == "bad_request"
    assert _index(tenant).lookup_customer_by_document(CPF).status is CustomerLookupStatus.NOT_FOUND


# --- privacy -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_database_holds_neither_document_nor_key(tenant):
    from app import db

    await _sync(tenant, [[_row(1, CPF, "2026-09-01T00:00:00")]])
    await _provider(tenant, FakeAdaptor()).execute("create_customer", _payload(OTHER_CPF))
    with db.get_conn() as conn, conn.cursor() as cur:
        dumped = ""
        for table in ("ai_mercos_customer_document_index", "ai_mercos_customer_index_config",
                      "ai_mercos_customer_creation_claim"):
            cur.execute(f"SELECT * FROM public.{table} WHERE tenant_id = %s", (tenant,))
            dumped += repr(cur.fetchall())
    for secret in (CPF, OTHER_CPF, "529.982.247-25", KEY_A):
        assert secret not in dumped
    # lookup exato: prefixo do documento nao encontra nada
    assert _index(tenant).lookup_customer_by_document(CPF[:10] + "0").status is not CustomerLookupStatus.FOUND
