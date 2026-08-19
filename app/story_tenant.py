"""Resolve tenant for Instagram Story flows from authenticated channel context.

Never accept tenant_id from the customer message body.
Never silently fall back to \"newstore\".
explicit_tenant_id requires a RequestPrincipal with access.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .config import get_settings
from .observability import log_event
from .request_principal import RequestPrincipal


class TenantResolution(BaseModel):
    ok: bool = False
    tenant_id: str | None = None
    source: Literal[
        "principal",
        "explicit",
        "integration",
        "instagram_account_map",
        "persona_settings",
        "unresolved",
    ] = "unresolved"
    failure_code: str | None = None
    instagram_account_id: str | None = None
    principal_subject: str | None = None


def _account_tenant_map() -> dict[str, str]:
    settings = get_settings()
    raw = str(getattr(settings, "instagram_story_account_tenant_map", "") or "")
    mapping: dict[str, str] = {}
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk or ":" not in chunk:
            continue
        account, tenant = chunk.split(":", 1)
        account_id = account.strip()
        tenant_id = tenant.strip()
        if account_id and tenant_id:
            mapping[account_id] = tenant_id
    # Single-store convenience: Meta IG business account → persona tenant.
    ig_id = str(getattr(settings, "meta_ig_business_account_id", "") or "").strip()
    default_tenant = str(getattr(settings, "agent_persona_tenant_id", "") or "").strip()
    if ig_id and default_tenant and ig_id not in mapping:
        mapping[ig_id] = default_tenant
    return mapping


async def resolve_story_tenant(
    *,
    provider: str,
    instagram_account_id: str,
    integration_id: str | None = None,
    explicit_tenant_id: str | None = None,
    principal: RequestPrincipal | None = None,
) -> TenantResolution:
    """Resolve tenant for Story catalog / association lookups."""
    _ = provider
    account = str(instagram_account_id or "").strip()
    explicit = str(explicit_tenant_id or "").strip()

    # 1) Authenticated principal with a single tenant or explicit allowed tenant.
    if principal is not None:
        if explicit:
            try:
                tid = principal.require_tenant(explicit)
            except PermissionError:
                log_event(
                    "story_tenant_resolution",
                    {"ok": False, "source": "principal", "code": "tenant_forbidden"},
                )
                return TenantResolution(
                    ok=False,
                    failure_code="tenant_forbidden",
                    source="unresolved",
                    instagram_account_id=account or None,
                    principal_subject=principal.subject_id,
                )
            return TenantResolution(
                ok=True,
                tenant_id=tid,
                source="principal",
                instagram_account_id=account or None,
                principal_subject=principal.subject_id,
            )
        if len(principal.tenant_ids) == 1 and "*" not in principal.tenant_ids:
            tid = next(iter(principal.tenant_ids))
            return TenantResolution(
                ok=True,
                tenant_id=tid,
                source="principal",
                instagram_account_id=account or None,
                principal_subject=principal.subject_id,
            )

    # 2) explicit only when principal already validated above; otherwise reject.
    if explicit and principal is None:
        log_event(
            "story_tenant_resolution",
            {"ok": False, "source": "explicit", "code": "explicit_requires_principal"},
        )
        return TenantResolution(
            ok=False,
            failure_code="explicit_requires_principal",
            source="unresolved",
            instagram_account_id=account or None,
        )

    # 3) Instagram account → tenant map (server config)
    mapping = _account_tenant_map()
    if account and account in mapping:
        resolution = TenantResolution(
            ok=True,
            tenant_id=mapping[account],
            source="instagram_account_map",
            instagram_account_id=account,
        )
        log_event("story_tenant_resolution", {"ok": True, "source": "instagram_account_map"})
        return resolution

    # 4) Integration map reserved
    if integration_id:
        log_event(
            "story_tenant_resolution",
            {"ok": False, "source": "integration", "code": "integration_map_missing"},
        )

    # 5) Persona settings bound to this deployment/integration (not customer-supplied)
    settings = get_settings()
    persona_tenant = str(getattr(settings, "agent_persona_tenant_id", None) or "").strip()
    if persona_tenant:
        resolution = TenantResolution(
            ok=True,
            tenant_id=persona_tenant,
            source="persona_settings",
            instagram_account_id=account or None,
        )
        log_event("story_tenant_resolution", {"ok": True, "source": "persona_settings"})
        return resolution

    log_event(
        "story_tenant_resolution",
        {
            "ok": False,
            "source": "unresolved",
            "code": "story_tenant_unresolved",
            "has_account": bool(account),
        },
    )
    return TenantResolution(
        ok=False,
        tenant_id=None,
        source="unresolved",
        failure_code="story_tenant_unresolved",
        instagram_account_id=account or None,
    )


def require_tenant_id(resolution: TenantResolution) -> str:
    if not resolution.ok or not resolution.tenant_id:
        raise ValueError(resolution.failure_code or "story_tenant_unresolved")
    return resolution.tenant_id
