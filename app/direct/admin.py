"""Authenticated preview, independent of production routing and channel sends."""
import base64
import hashlib
import hmac
import json
import time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.memory_scope import trusted_workspace
from app.models import IncomingMessage
from app.observability import log_event
from app.security import verify_admin_token
from app.direct.pipeline import run_direct_turn

router = APIRouter(tags=["direct-agent"], dependencies=[Depends(verify_admin_token)])


class PreviewRequest(BaseModel):
    workspace_id: UUID
    text: str = Field(min_length=1, max_length=8000)
    channel: Literal["whatsapp", "instagram"] = "whatsapp"
    session: str | None = Field(default=None, max_length=300000)


def encode_session(payload, secret):
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    return raw + "." + hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def decode_session(token, secret, workspace, channel):
    try:
        raw, signature = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(raw))
        if (payload["workspace"] != workspace or payload["channel"] != channel
                or payload["expires"] < time.time()):
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(400, detail="invalid_test_session") from None


@router.post("/api/test/direct")
async def direct_preview(payload: PreviewRequest):
    cfg = get_settings()
    workspace = trusted_workspace(cfg, str(payload.workspace_id))
    if not workspace:
        raise HTTPException(403, detail="workspace_mismatch")
    state = (decode_session(payload.session, cfg.admin_api_token, workspace, payload.channel)
             if payload.session else {"workspace": workspace, "channel": payload.channel,
                                      "id": "direct-test:" + uuid4().hex, "history": [], "previous": {}})
    incoming = IncomingMessage(workspace_id=workspace, provider="test", channel=payload.channel,
        sender_key=state["id"], conversation_id=state["id"], text=payload.text)
    try:
        result = await run_direct_turn(incoming, history=state["history"], previous=state["previous"])
    except Exception as exc:
        log_event("direct.preview.failed", {"error_type": type(exc).__name__})
        raise HTTPException(503, detail="direct_preview_unavailable") from None
    state["history"] = (state["history"] + [
        {"role": "user", "content": payload.text},
        {"role": "assistant", "content": result.reply_text}])[-20:]
    state["previous"] = result.response_metadata
    state["expires"] = time.time() + 3600
    metrics = result.response_metadata["direct_agent"]
    return {"ok": True, "engine": "direct", "active_engine": cfg.xnamai_agent_engine,
            "sent_to_customer": False, "reply_text": result.reply_text,
            "handoff_required": result.handoff_required,
            "session": encode_session(state, cfg.admin_api_token),
            "metrics": {key: metrics[key] for key in ("model", "calls", "tools", "input_tokens",
                        "output_tokens", "latency_ms", "prompt_version", "knowledge_document_count")}}
