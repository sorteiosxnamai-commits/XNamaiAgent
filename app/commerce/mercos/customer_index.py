"""Private, incremental customer-document index fed only by MercosAdaptor.

The MercosAdaptor has no lookup by CPF/CNPJ: ``GET /v1/customers`` only accepts
``alterado_apos``. The agent therefore keeps an incremental index of keyed
digests (HMAC-SHA-256, key from ``CUSTOMER_DOCUMENT_HMAC_KEY``), never the
document text nor the Mercos payload.

Absence is only provable after a FULL baseline (a sweep that started from an
empty cursor and reached the end) built with the current key, refreshed
recently. Anything else answers INDEX_NOT_READY — never NOT_FOUND.

Creation is serialised per tenant + document: a transaction-scoped advisory
lock (same pattern as ``app.db``/outbox) guards a re-lookup plus a durable
claim row, so two conversations confirming the same document produce at most
one POST, even before the next sync brings the new customer into the index.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import StrEnum
from typing import Any

from .sync import DEFAULT_MAX_PAGES, sync_resource
from .sync_state import DatabaseSyncStateStore, PROVIDER_NAME

RESOURCE = "customers"
TABLE = "public.ai_mercos_customer_document_index"
CONFIG_TABLE = "public.ai_mercos_customer_index_config"
CLAIM_TABLE = "public.ai_mercos_customer_creation_claim"
MAX_AGE = timedelta(hours=1)
MIN_KEY_LENGTH = 32


class CustomerLookupStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    #: The same document belongs to more than one customer: never pick one.
    AMBIGUOUS = "AMBIGUOUS"
    #: A creation for this document is in flight or its outcome is unknown.
    CREATION_PENDING = "CREATION_PENDING"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    #: No complete, recent baseline with the current key: absence is unknowable.
    INDEX_NOT_READY = "INDEX_NOT_READY"


@dataclass(frozen=True)
class CustomerLookupResult:
    status: CustomerLookupStatus
    customer_id: str | None = None

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "ok": self.status in {CustomerLookupStatus.FOUND, CustomerLookupStatus.NOT_FOUND},
            "status": self.status.value,
            "found": self.status == CustomerLookupStatus.FOUND,
            "customer_id": self.customer_id,
        }


@dataclass(frozen=True)
class CreationClaim:
    """``claimed`` means: this caller alone may POST, exactly once."""

    claimed: bool
    result: CustomerLookupResult


def document_digest(document: str, key: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", str(document or "").upper())
    if not (re.fullmatch(r"[0-9]{11}", normalized) or
            re.fullmatch(r"[A-Z0-9]{12}[0-9]{2}", normalized)) or len(key) < MIN_KEY_LENGTH:
        raise ValueError("invalid document or index key")
    return hmac.new(key.encode("utf-8"), normalized.encode("ascii"), hashlib.sha256).hexdigest()


def _key_fingerprint(key: str) -> str:
    return hmac.new(key.encode("utf-8"), b"mercos-customer-index-key-v1", hashlib.sha256).hexdigest()


def _lock_key(tenant_id: str, digest: str) -> str:
    return f"mercos-customer-document:{tenant_id}:{digest}"


def _failure_status(exc: Exception) -> CustomerLookupStatus:
    name = type(exc).__name__
    if name in {"QueryCanceled", "LockNotAvailable"} or isinstance(exc, TimeoutError):
        return CustomerLookupStatus.TIMEOUT
    return CustomerLookupStatus.PROVIDER_UNAVAILABLE


def _read_config(tenant_id: str) -> dict[str, Any] | None:
    from ...db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT key_fingerprint, baseline_completed_at FROM {CONFIG_TABLE} WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def _start_baseline(tenant_id: str, key: str) -> None:
    """Drop the tenant's index and restart from an empty cursor with ``key``."""
    from ...db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE} WHERE tenant_id = %s", (tenant_id,))
            # Claims are keyed digests too: resolved ones are obsolete with a
            # new key. Unresolved ones block the rebuild (see run_customer_sync).
            cur.execute(f"DELETE FROM {CLAIM_TABLE} WHERE tenant_id = %s AND status = 'created'", (tenant_id,))
            cur.execute(
                f"INSERT INTO {CONFIG_TABLE} (tenant_id, key_fingerprint, baseline_started_at, "
                "baseline_completed_at, updated_at) VALUES (%s, %s, now(), NULL, now()) "
                "ON CONFLICT (tenant_id) DO UPDATE SET key_fingerprint = EXCLUDED.key_fingerprint, "
                "baseline_started_at = now(), baseline_completed_at = NULL, updated_at = now()",
                (tenant_id, _key_fingerprint(key)),
            )


def _complete_baseline(tenant_id: str) -> None:
    from ...db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {CONFIG_TABLE} SET baseline_completed_at = now(), updated_at = now() "
                "WHERE tenant_id = %s AND baseline_completed_at IS NULL",
                (tenant_id,),
            )


def _unresolved_claims(tenant_id: str) -> int:
    from ...db import get_conn

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) AS n FROM {CLAIM_TABLE} WHERE tenant_id = %s AND status <> 'created'",
                (tenant_id,),
            )
            row = cur.fetchone()
    return int(row["n"]) if row else 0


def _resolve(cur: Any, tenant_id: str, digest: str) -> CustomerLookupResult:
    """Exact digest match only; index first, then the local creation claim."""
    cur.execute(
        f"SELECT customer_id FROM {TABLE} WHERE tenant_id = %s AND document_digest = %s "
        "ORDER BY customer_id LIMIT 2",
        (tenant_id, digest),
    )
    rows = cur.fetchall()
    if len(rows) > 1:
        return CustomerLookupResult(CustomerLookupStatus.AMBIGUOUS)
    if rows:
        return CustomerLookupResult(CustomerLookupStatus.FOUND, str(rows[0]["customer_id"]))
    cur.execute(
        f"SELECT status, customer_id FROM {CLAIM_TABLE} WHERE tenant_id = %s AND document_digest = %s",
        (tenant_id, digest),
    )
    claim = cur.fetchone()
    if claim:
        if claim["status"] == "created" and claim["customer_id"]:
            return CustomerLookupResult(CustomerLookupStatus.FOUND, str(claim["customer_id"]))
        return CustomerLookupResult(CustomerLookupStatus.CREATION_PENDING)
    return CustomerLookupResult(CustomerLookupStatus.NOT_FOUND)


class CustomerPageWriter:
    def __init__(self, *, tenant_id: str, hmac_key: str):
        self.tenant_id = tenant_id
        self.hmac_key = hmac_key

    async def write_page(self, resource: str, records: list[Any]) -> int:
        from ...db import get_conn

        if resource != RESOURCE:
            raise ValueError("wrong resource")
        with get_conn() as conn:
            with conn.cursor() as cur:
                for record in records:
                    row = record.raw
                    if not isinstance(row, dict):
                        raise ValueError("invalid customer row")
                    customer_id = str(record.external_id)
                    raw_document = str(row.get("cnpj") or "")
                    document = re.sub(r"[^A-Z0-9]", "", raw_document.upper())
                    # A document can change. Remove the old digest in the same
                    # transaction before inserting the current one.
                    cur.execute(
                        f"DELETE FROM {TABLE} WHERE tenant_id = %s AND customer_id = %s",
                        (self.tenant_id, customer_id),
                    )
                    # `excluido` is a documented Mercos customer field (checked
                    # against the production adaptor, tests/fixtures). Only an
                    # explicit True removes; absent/None keeps the customer.
                    if (row.get("excluido") is True or not document
                            or not (re.fullmatch(r"[0-9]{11}", document) or
                                    re.fullmatch(r"[A-Z0-9]{12}[0-9]{2}", document))):
                        continue
                    digest = document_digest(document, self.hmac_key)
                    cur.execute(
                        f"INSERT INTO {TABLE} (tenant_id, document_digest, customer_id, updated_at) "
                        "VALUES (%s, %s, %s, now()) "
                        "ON CONFLICT (tenant_id, document_digest, customer_id) "
                        "DO UPDATE SET updated_at = now()",
                        (self.tenant_id, digest, customer_id),
                    )
                    cur.execute(
                        f"UPDATE {CLAIM_TABLE} SET status = 'created', customer_id = %s, updated_at = now() "
                        "WHERE tenant_id = %s AND document_digest = %s "
                        "AND status = 'created_pending_sync'",
                        (customer_id, self.tenant_id, digest),
                    )
        return len(records)


class CustomerDocumentIndex:
    def __init__(self, *, tenant_id: str, hmac_key: str, state_store: Any | None = None):
        self.tenant_id = tenant_id
        self.hmac_key = hmac_key
        self.state_store = state_store or DatabaseSyncStateStore(tenant_id=tenant_id)

    def not_ready_reason(self) -> str | None:
        """``None`` when absence can be trusted; otherwise why it cannot."""
        if len(self.hmac_key) < MIN_KEY_LENGTH:
            return "key_not_configured"
        try:
            config = _read_config(self.tenant_id)
            state = self.state_store.read(PROVIDER_NAME, RESOURCE)
        except Exception:  # noqa: BLE001 - readiness never raises
            return "index_unavailable"
        if not config:
            return "never_synced"
        if config.get("key_fingerprint") != _key_fingerprint(self.hmac_key):
            return "key_rotated_rebuild_required"
        if config.get("baseline_completed_at") is None:
            return "baseline_in_progress"
        last = state.last_success_at
        if last is None or state.last_error_code:
            return "last_sync_failed"
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last > MAX_AGE:
            return "stale"
        return None

    @property
    def ready(self) -> bool:
        return self.not_ready_reason() is None

    def lookup_customer_by_document(self, document: str) -> CustomerLookupResult:
        if not self.ready:
            return CustomerLookupResult(CustomerLookupStatus.INDEX_NOT_READY)
        try:
            digest = document_digest(document, self.hmac_key)
        except ValueError:
            return CustomerLookupResult(CustomerLookupStatus.INVALID_RESPONSE)
        try:
            from ...db import get_conn

            with get_conn() as conn:
                with conn.cursor() as cur:
                    return _resolve(cur, self.tenant_id, digest)
        except Exception as exc:  # noqa: BLE001 - lookup failure is a status
            return CustomerLookupResult(_failure_status(exc))

    def claim_creation(self, document: str) -> CreationClaim:
        """Re-lookup under the per-document lock and claim the single POST."""
        if not self.ready:
            return CreationClaim(False, CustomerLookupResult(CustomerLookupStatus.INDEX_NOT_READY))
        try:
            digest = document_digest(document, self.hmac_key)
        except ValueError:
            return CreationClaim(False, CustomerLookupResult(CustomerLookupStatus.INVALID_RESPONSE))
        try:
            from ...db import get_conn

            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (_lock_key(self.tenant_id, digest),),
                    )
                    result = _resolve(cur, self.tenant_id, digest)
                    if result.status != CustomerLookupStatus.NOT_FOUND:
                        return CreationClaim(False, result)
                    cur.execute(
                        f"INSERT INTO {CLAIM_TABLE} (tenant_id, document_digest, status) "
                        "VALUES (%s, %s, 'creating')",
                        (self.tenant_id, digest),
                    )
            return CreationClaim(True, result)
        except Exception as exc:  # noqa: BLE001 - no claim, no POST
            return CreationClaim(False, CustomerLookupResult(_failure_status(exc)))

    def finish_creation(self, document: str, *, outcome: str, customer_id: str | None = None) -> None:
        """Record the POST outcome: ``created``, ``unknown`` or ``released``.

        ``released`` (definitive rejection, nothing was created) frees the
        document for a later attempt. A failure here leaves the claim as
        ``creating``, which keeps blocking — the safe side.
        """
        digest = document_digest(document, self.hmac_key)
        from ...db import get_conn

        with get_conn() as conn:
            with conn.cursor() as cur:
                if outcome == "released":
                    cur.execute(
                        f"DELETE FROM {CLAIM_TABLE} WHERE tenant_id = %s AND document_digest = %s "
                        "AND status = 'creating'",
                        (self.tenant_id, digest),
                    )
                    return
                if outcome not in {"created", "created_pending_sync", "unknown"}:
                    raise ValueError("invalid creation outcome")
                cur.execute(
                    f"UPDATE {CLAIM_TABLE} SET status = %s, customer_id = %s, updated_at = now() "
                    "WHERE tenant_id = %s AND document_digest = %s",
                    (outcome, customer_id, self.tenant_id, digest),
                )


async def run_customer_sync(*, settings: Any | None = None, client: Any | None = None,
                            store: Any | None = None, writer: Any | None = None,
                            max_pages: int = DEFAULT_MAX_PAGES) -> dict[str, Any]:
    from ...config import get_settings
    from .client import MercosAdaptorClient

    settings = settings or get_settings()
    if not getattr(settings, "mercos_adaptor_configured", False):
        return {"ok": False, "error": "commerce_adaptor_not_configured"}
    if not getattr(settings, "database_url", ""):
        return {"ok": False, "error": "database_not_configured"}
    key = getattr(settings, "customer_document_hmac_key", "") or ""
    if len(key) < MIN_KEY_LENGTH:
        return {"ok": False, "error": "customer_index_key_not_configured"}
    tenant_id = settings.commerce_tenant_id
    store = store or DatabaseSyncStateStore(tenant_id=tenant_id)
    if store.read(PROVIDER_NAME, RESOURCE).missing_table:
        return {"ok": False, "error": "sync_state_table_missing"}
    try:
        config = _read_config(tenant_id)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "customer_index_table_missing"}

    rebuilding = not config or config.get("key_fingerprint") != _key_fingerprint(key)
    if rebuilding:
        # First run, or the key changed: every stored digest is useless. Start
        # a fresh FULL baseline from an empty cursor; lookups stay
        # INDEX_NOT_READY until it reaches the end.
        try:
            if config and _unresolved_claims(tenant_id):
                return {"ok": False, "error": "unresolved_creation_claims_block_rebuild"}
            _start_baseline(tenant_id, key)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "customer_index_rebuild_failed"}
        store.set_cursor(PROVIDER_NAME, RESOURCE, None)

    client = client or MercosAdaptorClient(
        base_url=settings.mercos_adaptor_url, api_key=settings.mercos_adaptor_api_key,
        timeout_seconds=settings.mercos_adaptor_timeout_seconds,
    )
    writer = writer or CustomerPageWriter(tenant_id=tenant_id, hmac_key=key)
    outcome = await sync_resource(client=client, resource=RESOURCE, store=store,
                                  writer=writer, max_pages=max_pages, strict_records=True)
    complete = outcome.ok and outcome.stopped_reason is None and outcome.skipped == 0
    if complete:
        try:
            _complete_baseline(tenant_id)
        except Exception:  # noqa: BLE001
            store.record_failure(PROVIDER_NAME, RESOURCE, error_code="index_config_write_failed")
            return {**outcome.as_log(), "ok": False, "error": "index_config_write_failed"}
        store.record_success(PROVIDER_NAME, RESOURCE, records=outcome.records, pages=outcome.pages)
    else:
        store.record_failure(PROVIDER_NAME, RESOURCE,
                             error_code=outcome.error_code or outcome.stopped_reason or "invalid_response")
    return {**outcome.as_log(), "ok": complete, "baseline_started": rebuilding}
