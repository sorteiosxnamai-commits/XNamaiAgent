"""Bound webhook bodies while preserving the exact signed bytes."""
from fastapi import HTTPException, Request

MAX_REQUEST_BODY_BYTES = 1024 * 1024


async def read_limited_request_body(request: Request, *, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > max_bytes:
        raise HTTPException(413, detail="request_body_too_large")
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > max_bytes:
            raise HTTPException(413, detail="request_body_too_large")
        chunks.extend(chunk)
    body = bytes(chunks)
    request._body = body
    return body
