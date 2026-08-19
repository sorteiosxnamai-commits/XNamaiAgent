"""Authenticated request principal for admin / internal Story operations.

Never trust tenant_id, confirmed_by, or actor from unauthenticated client bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RequestPrincipal:
    subject_id: str
    tenant_ids: frozenset[str]
    roles: frozenset[str]
    source: str

    def can_access_tenant(self, tenant_id: str) -> bool:
        tid = str(tenant_id or "").strip()
        if not tid:
            return False
        if "admin" in self.roles or "system" in self.roles:
            # Still require an explicit allowed tenant set (may be "*").
            return tid in self.tenant_ids or "*" in self.tenant_ids
        return tid in self.tenant_ids

    def require_tenant(self, tenant_id: str) -> str:
        tid = str(tenant_id or "").strip()
        if not self.can_access_tenant(tid):
            raise PermissionError("tenant_forbidden")
        return tid


def principal_from_admin_token(
    *,
    subject_id: str = "admin_token",
    tenant_ids: Iterable[str] | None = None,
    roles: Iterable[str] | None = None,
) -> RequestPrincipal:
    """Build principal for shared admin Bearer auth.

    Tenant set comes from server config / headers validated by the API layer —
    never from the client body.
    """
    tenants = frozenset(str(t).strip() for t in (tenant_ids or ()) if str(t).strip())
    role_set = frozenset(str(r).strip() for r in (roles or ("admin",)) if str(r).strip())
    return RequestPrincipal(
        subject_id=str(subject_id or "admin_token").strip()[:120] or "admin_token",
        tenant_ids=tenants or frozenset({"*"}),
        roles=role_set or frozenset({"admin"}),
        source="admin_bearer",
    )


def principal_from_internal(
    *,
    subject_id: str,
    tenant_id: str,
    source: str = "internal",
) -> RequestPrincipal:
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_required")
    return RequestPrincipal(
        subject_id=str(subject_id or "internal").strip()[:120] or "internal",
        tenant_ids=frozenset({tid}),
        roles=frozenset({"system"}),
        source=source,
    )
