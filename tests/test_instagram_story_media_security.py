"""Offline SSRF / DNS / streaming tests for Instagram Story media (no real network)."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest

from app.instagram_story_media import (
    StoryMediaError,
    download_story_media,
    validate_story_media_url,
    _sniff_mime,
)
from app.instagram_story_parser import safe_media_reference, strip_signed_url


def _gai_for(*ips: str):
    def _fake(host, port, *args, **kwargs):
        _ = (host, port, args, kwargs)
        results = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            results.append((family, socket.SOCK_STREAM, 0, "", (ip, 443)))
        return results

    return _fake


@pytest.fixture
def allow_cdn(monkeypatch):
    """Mock DNS to a public IPv4 for allowed Instagram CDN hosts."""
    monkeypatch.setattr(
        "app.instagram_story_media.socket.getaddrinfo",
        _gai_for("157.240.0.1"),
    )


def test_signed_url_preserved_with_mocked_public_dns(allow_cdn):
    url = "https://scontent.cdninstagram.com/v/t51/x.jpg?oe=ABC&oh=SECRET"
    cleaned, ips = validate_story_media_url(url)
    assert cleaned == url
    assert "oh=SECRET" in cleaned
    assert ips == ["157.240.0.1"]


def test_public_ipv6_allowed(monkeypatch):
    monkeypatch.setattr(
        "app.instagram_story_media.socket.getaddrinfo",
        _gai_for("2a03:2880:f000::1"),
    )
    url = "https://scontent.cdninstagram.com/v/t51/x.jpg?oe=1&oh=tok"
    cleaned, ips = validate_story_media_url(url)
    assert cleaned == url
    assert ips == ["2a03:2880:f000::1"]


@pytest.mark.parametrize(
    "ip,code",
    [
        ("10.0.0.5", "private_ip_blocked"),
        ("127.0.0.1", "private_ip_blocked"),
        ("169.254.1.1", "private_ip_blocked"),
        ("192.168.1.10", "private_ip_blocked"),
        ("172.16.0.8", "private_ip_blocked"),
        ("0.0.0.0", "private_ip_blocked"),
        ("224.0.0.1", "private_ip_blocked"),
        ("169.254.169.254", "private_ip_blocked"),
    ],
)
def test_blocked_resolved_ips(monkeypatch, ip, code):
    monkeypatch.setattr(
        "app.instagram_story_media.socket.getaddrinfo",
        _gai_for(ip),
    )
    with pytest.raises(StoryMediaError) as exc:
        validate_story_media_url("https://scontent.cdninstagram.com/v/t.jpg")
    assert exc.value.code == code


def test_dns_empty_raises(monkeypatch):
    monkeypatch.setattr(
        "app.instagram_story_media.socket.getaddrinfo",
        lambda *a, **k: [],
    )
    with pytest.raises(StoryMediaError) as exc:
        validate_story_media_url("https://scontent.cdninstagram.com/v/t.jpg")
    assert exc.value.code == "dns_failed"


def test_dns_exception_raises(monkeypatch):
    def _boom(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr("app.instagram_story_media.socket.getaddrinfo", _boom)
    with pytest.raises(StoryMediaError) as exc:
        validate_story_media_url("https://scontent.cdninstagram.com/v/t.jpg")
    assert exc.value.code == "dns_failed"
    assert "oh=" not in str(exc.value)


def test_sanitized_log_reference_has_no_signature():
    url = "https://scontent.cdninstagram.com/v/t51/x.jpg?oe=ABC&oh=SECRET"
    ref = safe_media_reference(url)
    assert ref is not None
    dumped = ref.model_dump(mode="json")
    assert "SECRET" not in str(dumped)
    assert "oh=" not in str(dumped)
    cleaned = strip_signed_url(url)
    assert cleaned is not None
    assert "oh=" not in cleaned


@pytest.mark.asyncio
async def test_redirect_public_allowed(monkeypatch):
    from app import instagram_story_media as media_mod

    monkeypatch.setattr(media_mod.socket, "getaddrinfo", _gai_for("157.240.0.1"))
    monkeypatch.setenv("INSTAGRAM_STORY_MEDIA_STORAGE_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()

    calls = {"n": 0}

    class FakeStream:
        def __init__(self, url: str, **_kwargs):
            self.url = url
            calls["n"] += 1
            if calls["n"] == 1:
                self.status_code = 302
                self.headers = {
                    "location": "https://scontent.cdninstagram.com/v/t51/final.jpg?oh=NEW"
                }
            else:
                self.status_code = 200
                self.headers = {"content-type": "image/jpeg", "content-length": "4"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            if self.status_code == 200:
                yield b"\xff\xd8\xff\xe0"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            assert kwargs.get("follow_redirects") is False
            return FakeStream(url)

    monkeypatch.setattr(media_mod.httpx, "AsyncClient", FakeClient)
    result = await download_story_media(
        "https://scontent.cdninstagram.com/v/t51/start.jpg?oh=OLD",
        tenant_id="t1",
    )
    assert result.content.startswith(b"\xff\xd8")
    assert calls["n"] == 2
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_redirect_private_blocked(monkeypatch):
    from app import instagram_story_media as media_mod

    def _gai(host, *a, **k):
        if host == "evil.internal":
            return _gai_for("10.0.0.9")(host, 443)
        return _gai_for("157.240.0.1")(host, 443)

    monkeypatch.setattr(media_mod.socket, "getaddrinfo", _gai)
    # Allow evil.internal via settings would still fail private IP — use CDN host
    # that redirects to a private IP by resolving differently. Simulate via
    # validate failing on redirect target host not allowed OR private.
    monkeypatch.setenv("INSTAGRAM_STORY_MEDIA_STORAGE_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()

    class FakeStream:
        def __init__(self, url: str, **_kwargs):
            self.status_code = 302
            self.headers = {"location": "https://scontent.cdninstagram.com/private.jpg"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            if False:  # pragma: no cover
                yield b""

    # Make second validate see private IP for same host by flipping DNS after first call
    state = {"n": 0}

    def _gai2(host, *a, **k):
        state["n"] += 1
        if state["n"] == 1:
            return _gai_for("157.240.0.1")(host, 443)
        return _gai_for("10.1.1.1")(host, 443)

    monkeypatch.setattr(media_mod.socket, "getaddrinfo", _gai2)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            return FakeStream(url)

    monkeypatch.setattr(media_mod.httpx, "AsyncClient", FakeClient)
    with pytest.raises(StoryMediaError) as exc:
        await download_story_media(
            "https://scontent.cdninstagram.com/v/start.jpg",
            tenant_id="t1",
        )
    assert exc.value.code in {"redirect_private_ip", "private_ip_blocked"}
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_redirect_limit_exceeded(monkeypatch):
    from app import instagram_story_media as media_mod

    monkeypatch.setattr(media_mod.socket, "getaddrinfo", _gai_for("157.240.0.1"))
    monkeypatch.setenv("INSTAGRAM_STORY_MEDIA_STORAGE_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    n = {"i": 0}

    class FakeStream:
        def __init__(self, url: str, **_kwargs):
            n["i"] += 1
            self.status_code = 302
            self.headers = {
                "location": f"https://scontent.cdninstagram.com/v/hop{n['i']}.jpg"
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            if False:  # pragma: no cover
                yield b""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            return FakeStream(url)

    monkeypatch.setattr(media_mod.httpx, "AsyncClient", FakeClient)
    with pytest.raises(StoryMediaError) as exc:
        await download_story_media(
            "https://scontent.cdninstagram.com/v/start.jpg",
            tenant_id="t1",
        )
    assert exc.value.code == "redirect_limit_exceeded"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_mime_mismatch_and_oversize(monkeypatch):
    from app import instagram_story_media as media_mod
    from app.config import get_settings

    monkeypatch.setattr(media_mod.socket, "getaddrinfo", _gai_for("157.240.0.1"))
    monkeypatch.setenv("INSTAGRAM_STORY_MEDIA_STORAGE_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "instagram_story_media_max_bytes", 20)
    monkeypatch.setattr(settings, "instagram_story_media_storage_enabled", False)

    class FakeStream:
        def __init__(self, url: str, **_kwargs):
            self.status_code = 200
            self.headers = {
                "content-type": "image/jpeg",
                # Content-Length absent — stream must still enforce limit
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            yield b"<!DOCTYPE html>" + b"x" * 30

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            return FakeStream(url)

    monkeypatch.setattr(media_mod.httpx, "AsyncClient", FakeClient)
    with pytest.raises(StoryMediaError) as exc:
        await download_story_media(
            "https://scontent.cdninstagram.com/v/t.jpg",
            tenant_id="t1",
        )
    assert exc.value.code in {"file_too_large", "html_disguised", "mime_mismatch", "mime_invalid"}
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_content_length_too_large(monkeypatch):
    from app import instagram_story_media as media_mod
    from app.config import get_settings

    monkeypatch.setattr(media_mod.socket, "getaddrinfo", _gai_for("157.240.0.1"))
    monkeypatch.setenv("INSTAGRAM_STORY_MEDIA_STORAGE_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "instagram_story_media_max_bytes", 100)
    monkeypatch.setattr(settings, "instagram_story_media_storage_enabled", False)

    class FakeStream:
        def __init__(self, url: str, **_kwargs):
            self.status_code = 200
            self.headers = {
                "content-type": "image/jpeg",
                "content-length": "999999",
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            yield b"\xff\xd8\xff\xe0"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            return FakeStream(url)

    monkeypatch.setattr(media_mod.httpx, "AsyncClient", FakeClient)
    with pytest.raises(StoryMediaError) as exc:
        await download_story_media(
            "https://scontent.cdninstagram.com/v/t.jpg",
            tenant_id="t1",
        )
    assert exc.value.code == "file_too_large"
    get_settings.cache_clear()


def test_sniff_mime_types():
    assert _sniff_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert _sniff_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert _sniff_mime(b"GIF89a....") == "image/gif"
    assert _sniff_mime(b"RIFF....WEBP") == "image/webp"
