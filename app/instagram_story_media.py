"""Safe Instagram Story media download (SSRF-hardened, streaming).

Operational URLs keep signed query strings. Logs only use SafeMediaReference.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx

from .config import get_settings
from .instagram_story_parser import safe_media_reference
from .observability import log_event


_DEFAULT_ALLOWED_SUFFIXES = (
    "fbcdn.net",
    "cdninstagram.com",
    "instagram.com",
    "facebook.com",
    "fbsbx.com",
    "brevo.com",
    "sendinblue.com",
    "sibpages.com",
)

_DANGEROUS_HOST_GLOBS = ("*", "*.*", "0.0.0.0", "::")


@dataclass
class DownloadedStoryMedia:
    content: bytes
    content_type: str
    sha256: str
    final_host: str
    storage_path: str | None = None
    byte_count: int = 0


class StoryMediaError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class StoryMediaStorage(Protocol):
    async def put_private(
        self,
        *,
        content: bytes,
        content_type: str,
        sha256: str,
        tenant_id: str,
    ) -> str | None: ...

    async def get_private(self, *, storage_path: str) -> bytes | None: ...

    async def delete(self, *, storage_path: str) -> bool: ...

    async def exists(self, *, storage_path: str) -> bool: ...


class SupabasePrivateStoryMediaStorage:
    """Private object storage under a non-public prefix. Never returns signed URLs."""

    async def put_private(
        self,
        *,
        content: bytes,
        content_type: str,
        sha256: str,
        tenant_id: str,
    ) -> str | None:
        settings = get_settings()
        if not settings.supabase_url or not settings.supabase_service_key:
            return None
        bucket = str(
            getattr(settings, "instagram_story_storage_bucket", None)
            or getattr(settings, "supabase_story_media_bucket", None)
            or ""
        ).strip()
        if not bucket:
            # Do not reuse the public audio bucket as a silent fallback.
            return None
        object_name = f"private/instagram-stories/{tenant_id}/{sha256[:48]}"
        upload_url = (
            f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
            f"{bucket}/{object_name}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    upload_url,
                    content=content,
                    headers={
                        "Authorization": f"Bearer {settings.supabase_service_key}",
                        "Content-Type": content_type,
                        "x-upsert": "true",
                    },
                )
                if response.status_code >= 400:
                    log_event(
                        "instagram_story.media_storage_failed",
                        {"code": f"http_{response.status_code}"},
                    )
                    return None
            return f"supabase://{bucket}/{object_name}"
        except Exception as exc:  # noqa: BLE001
            log_event(
                "instagram_story.media_storage_failed",
                {"code": type(exc).__name__},
            )
            return None

    async def get_private(self, *, storage_path: str) -> bytes | None:
        return None  # reserved — callers should not need public fetch

    async def delete(self, *, storage_path: str) -> bool:
        settings = get_settings()
        if not storage_path.startswith("supabase://"):
            return False
        if not settings.supabase_url or not settings.supabase_service_key:
            return False
        try:
            _, rest = storage_path.split("supabase://", 1)
            bucket, object_name = rest.split("/", 1)
        except ValueError:
            return False
        delete_url = (
            f"{settings.supabase_url.rstrip('/')}/storage/v1/object/"
            f"{bucket}/{object_name}"
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.delete(
                    delete_url,
                    headers={"Authorization": f"Bearer {settings.supabase_service_key}"},
                )
            return response.status_code < 400
        except Exception:
            return False

    async def exists(self, *, storage_path: str) -> bool:
        return bool(storage_path)


def _configured_allowed_suffixes() -> list[str]:
    settings = get_settings()
    extras: list[str] = []
    for part in (getattr(settings, "instagram_story_allowed_hosts", "") or "").split(","):
        host = part.strip().casefold().lstrip(".")
        if not host:
            continue
        if host in _DANGEROUS_HOST_GLOBS or "*" in host or host.startswith("."):
            raise StoryMediaError("allowed_hosts_invalid")
        if "/" in host or " " in host:
            raise StoryMediaError("allowed_hosts_invalid")
        extras.append(host)
    return extras + list(_DEFAULT_ALLOWED_SUFFIXES)


def _allowed_host(host: str) -> bool:
    host_l = host.casefold().rstrip(".")
    for suffix in _configured_allowed_suffixes():
        if host_l == suffix or host_l.endswith("." + suffix):
            return True
    return False


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or str(ip) in {"169.254.169.254", "metadata.google.internal"}
    )


def validate_story_media_url(url: str) -> tuple[str, list[str]]:
    """Validate operational URL. Returns (url, resolved_ip_strings).

    Preserves the full signed URL. DNS rebinding residual risk is documented:
    we resolve once before connect; httpx may re-resolve — prefer egress allowlist
    in production.
    """
    text = (url or "").strip()
    if not text:
        raise StoryMediaError("url_missing")
    parsed = urlparse(text)
    if parsed.scheme != "https":
        raise StoryMediaError("scheme_not_https")
    host = parsed.hostname or ""
    if not host:
        raise StoryMediaError("host_missing")
    if host.casefold() in {"localhost", "metadata", "metadata.google.internal"}:
        raise StoryMediaError("host_blocked")
    if not _allowed_host(host):
        raise StoryMediaError("host_not_allowed")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise StoryMediaError("dns_failed") from exc
    resolved: list[str] = []
    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise StoryMediaError("private_ip_blocked")
        resolved.append(str(ip))
    if not resolved:
        raise StoryMediaError("dns_failed")
    return text, resolved


def _sniff_mime(content: bytes) -> str | None:
    if not content:
        return None
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        return "image/webp"
    if b"ftyp" in content[:64]:
        return "video/mp4"
    if content.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html", b"<svg")):
        return "text/html"
    if content[:2] in {b"MZ", b"\x7fE"}:
        return "application/octet-stream"
    return None


async def _stream_once(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
) -> tuple[int, bytes, str, str]:
    async with client.stream(
        "GET",
        url,
        follow_redirects=False,
        headers={"User-Agent": "NSAgentStoryMedia/2.0"},
    ) as response:
        status = response.status_code
        if status in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            return status, b"", "", location or ""
        if status >= 400:
            raise StoryMediaError(f"http_{status}")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise StoryMediaError("file_too_large")
            except ValueError:
                pass
        header_ct = str(response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if header_ct and not (
            header_ct.startswith("image/")
            or header_ct.startswith("video/")
            or header_ct in {"application/octet-stream"}
        ):
            raise StoryMediaError("mime_invalid")
        total = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise StoryMediaError("file_too_large")
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content:
            raise StoryMediaError("empty_body")
        return status, content, header_ct, ""


async def download_story_media(
    url: str,
    *,
    tenant_id: str = "unknown",
    storage: StoryMediaStorage | None = None,
) -> DownloadedStoryMedia:
    settings = get_settings()
    max_bytes = int(getattr(settings, "instagram_story_media_max_bytes", 12_582_912) or 12_582_912)
    timeout = float(getattr(settings, "instagram_story_media_timeout_seconds", 10) or 10)
    max_redirects = 3

    current, _resolved = validate_story_media_url(url)
    seen: set[str] = set()
    log_ref = safe_media_reference(current)
    log_event(
        "instagram_story.media_download_started",
        {
            "host": log_ref.host if log_ref else None,
            "path_hash": log_ref.path_hash if log_ref else None,
            "max_bytes": max_bytes,
        },
    )

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            redirects = 0
            while True:
                if current in seen:
                    raise StoryMediaError("redirect_loop")
                seen.add(current)
                status, content, header_ct, location = await _stream_once(
                    client, current, max_bytes=max_bytes
                )
                if status in {301, 302, 303, 307, 308}:
                    redirects += 1
                    if redirects > max_redirects:
                        raise StoryMediaError("redirect_limit_exceeded")
                    if not location:
                        raise StoryMediaError("redirect_missing_location")
                    next_url = urljoin(current, location)
                    try:
                        current, _ = validate_story_media_url(next_url)
                    except StoryMediaError as exc:
                        if exc.code == "host_not_allowed":
                            raise StoryMediaError("redirect_host_not_allowed") from exc
                        if exc.code == "private_ip_blocked":
                            raise StoryMediaError("redirect_private_ip") from exc
                        raise
                    continue

                sniffed = _sniff_mime(content)
                if sniffed == "text/html":
                    raise StoryMediaError("html_disguised")
                if sniffed is None and not (
                    header_ct.startswith("image/") or header_ct.startswith("video/")
                ):
                    raise StoryMediaError("mime_invalid")
                content_type = sniffed or header_ct or "application/octet-stream"
                if not (
                    content_type.startswith("image/") or content_type.startswith("video/")
                ):
                    raise StoryMediaError("mime_invalid")
                if header_ct.startswith("image/") and sniffed and not sniffed.startswith("image/"):
                    raise StoryMediaError("mime_mismatch")
                if header_ct.startswith("video/") and sniffed and not sniffed.startswith("video/"):
                    raise StoryMediaError("mime_mismatch")

                digest = hashlib.sha256(content).hexdigest()
                storage_path = None
                if bool(getattr(settings, "instagram_story_media_storage_enabled", True)):
                    backend = storage or SupabasePrivateStoryMediaStorage()
                    storage_path = await backend.put_private(
                        content=content,
                        content_type=content_type,
                        sha256=digest,
                        tenant_id=tenant_id,
                    )
                    # Never invent local:// paths when nothing was stored.
                return DownloadedStoryMedia(
                    content=content,
                    content_type=content_type,
                    sha256=digest,
                    final_host=urlparse(current).hostname or "unknown",
                    storage_path=storage_path,
                    byte_count=len(content),
                )
    except StoryMediaError as exc:
        log_event(
            "instagram_story.media_download_failed",
            {
                "code": exc.code,
                "host": log_ref.host if log_ref else None,
                "path_hash": log_ref.path_hash if log_ref else None,
            },
        )
        raise
    except Exception as exc:  # noqa: BLE001
        log_event(
            "instagram_story.media_download_failed",
            {
                "code": type(exc).__name__,
                "host": log_ref.host if log_ref else None,
            },
        )
        raise StoryMediaError("download_failed") from exc


def extract_video_frames_best_effort(
    content: bytes,
    *,
    max_frames: int = 3,
) -> list[bytes]:
    """Video frame extraction — disabled until a deploy-compatible decoder ships.

    Set INSTAGRAM_STORY_VIDEO_FRAME_ANALYSIS_ENABLED=true only after OpenCV/ffmpeg
    is validated on Render. Until then this returns [].
    """
    settings = get_settings()
    if not bool(getattr(settings, "instagram_story_video_frame_analysis_enabled", False)):
        return []
    _ = content
    _ = max_frames
    # Intentionally empty: do not pretend frames were extracted.
    return []


def safe_media_url_for_log(url: str | None) -> dict[str, Any]:
    ref = safe_media_reference(url)
    if ref is None:
        return {"present": False}
    return ref.model_dump(mode="json")
