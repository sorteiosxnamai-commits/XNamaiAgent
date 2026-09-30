"""Namespaced summary keys; never reuse an unscoped customer summary."""
import hashlib
import json


def summary_conversation_key(incoming, settings, *, conversation_key=None):
    if incoming is None:
        return None
    workspace = getattr(incoming, "workspace_id", None) or getattr(settings, "chatbo_workspace_id", None)
    identity = conversation_key or incoming.conversation_id or incoming.sender_key or incoming.sender_phone
    if not workspace or not identity:
        return None
    scope = [str(workspace), str(incoming.channel or "unknown"), str(identity)]
    digest = hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"summary:v2:{digest}"
