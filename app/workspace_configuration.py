"""Load the versioned Mai Agent settings owned by the XNamai workspace."""

from __future__ import annotations

from typing import Any

from app.db import connect_database_url


def _apply_overrides(settings: Any, overrides: dict[str, Any]) -> Any:
    allowed = set(type(settings).model_fields)
    safe = {key: value for key, value in overrides.items() if key in allowed}
    return settings.model_copy(update=safe) if safe else settings


def load_workspace_settings(settings: Any) -> Any:
    """Overlay only catalog-approved setting attributes; fail closed to env config."""
    database_url = str(getattr(settings, "database_url", "") or "").strip()
    workspace_id = str(getattr(settings, "chatbo_workspace_id", "") or "").strip()
    if not database_url or not workspace_id:
        return settings

    try:
        with connect_database_url(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT catalog.definition->>'attribute' AS attribute,
                           value.value AS configured_value
                      FROM public.workspace_agents AS agent
                      CROSS JOIN LATERAL jsonb_each(
                        COALESCE(agent.configuration #> '{runtime,values}', '{}'::jsonb)
                      ) AS value(key, value)
                      JOIN public.agent_configuration_catalog AS catalog
                        ON catalog.key = value.key
                     WHERE agent.workspace_id = %s::uuid
                       AND agent.agent_type = 'mai_agent'
                       AND agent.status = 'active'
                       AND catalog.definition->>'target' = 'setting'
                       AND COALESCE(catalog.definition->>'attribute', '') <> ''
                    """,
                    (workspace_id,),
                )
                overrides = {
                    str(row["attribute"]): row["configured_value"]
                    for row in cur.fetchall()
                }
        return _apply_overrides(settings, overrides)
    except Exception as exc:  # noqa: BLE001 - env settings remain the safe fallback
        print(
            "[workspace.configuration]",
            {"event": "load_failed", "error_type": type(exc).__name__},
        )
        return settings
