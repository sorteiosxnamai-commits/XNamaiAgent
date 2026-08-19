from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx
from openai import APIStatusError, OpenAI

from .config import get_settings
from .openai_runtime import execute_openai_call_sync

AUDIO_MIME_PREFIXES = ("audio/",)
AUDIO_MIME_TYPES = {
    "application/ogg",
    "application/octet-stream",
}
AUDIO_EXTENSIONS = (".ogg", ".opus", ".mp3", ".m4a", ".aac", ".amr", ".wav", ".webm")


def is_placeholder_audio_text(text: str | None, filename: str | None = None) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return True
    if filename and normalized == filename.strip().lower():
        return True
    placeholders = (
        "audio",
        "voice note",
        "voice message",
        "nota de voz",
        "mensagem de voz",
        "ptt",
    )
    if normalized in placeholders:
        return True
    return normalized.endswith(AUDIO_EXTENSIONS)


def should_transcribe_incoming(text: str | None, audio_url: str | None, filename: str | None = None) -> bool:
    return bool(audio_url)


def is_audio_attachment(file_obj: dict[str, Any] | None) -> bool:
    if not file_obj:
        return False

    mime = (file_obj.get("mimeType") or file_obj.get("mimetype") or "").lower().strip()
    name = (file_obj.get("name") or "").lower().strip()

    if mime.startswith(AUDIO_MIME_PREFIXES) or mime in AUDIO_MIME_TYPES:
        return True
    return any(name.endswith(ext) for ext in AUDIO_EXTENSIONS)


def extract_audio_attachment(payload: dict[str, Any]) -> dict[str, Any] | None:
    messages = payload.get("messages")
    candidates: list[dict[str, Any]] = []

    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("type") != "visitor":
                continue
            file_obj = message.get("file")
            if isinstance(file_obj, dict):
                candidates.append(file_obj)
            attachments = message.get("attachments")
            if isinstance(attachments, list):
                candidates.extend(item for item in attachments if isinstance(item, dict))

    for key in ("file",):
        file_obj = payload.get(key)
        if isinstance(file_obj, dict):
            candidates.append(file_obj)

    for file_obj in candidates:
        if is_audio_attachment(file_obj) and file_obj.get("link"):
            return file_obj
    return None


async def download_audio_file(url: str) -> tuple[bytes, str]:
    headers = {
        "User-Agent": "NewStoreAgent/1.0",
        "Accept": "audio/*,*/*",
    }
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()

    content_type = (response.headers.get("content-type") or "audio/ogg").split(";")[0].strip()
    if not content_type.startswith("audio/") and content_type not in AUDIO_MIME_TYPES:
        content_type = "audio/ogg"
    return response.content, content_type


def _extension_for_content_type(content_type: str, fallback_name: str | None = None) -> str:
    mapping = {
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/amr": ".amr",
        "audio/wav": ".wav",
        "audio/webm": ".webm",
        "application/ogg": ".ogg",
    }
    if content_type in mapping:
        return mapping[content_type]
    if fallback_name and "." in fallback_name:
        return Path(fallback_name).suffix
    return ".ogg"


async def transcribe_audio_url(url: str, filename: str | None = None) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("openai_api_key_missing")

    audio_bytes, content_type = await download_audio_file(url)
    suffix = _extension_for_content_type(content_type, filename)

    client = OpenAI(api_key=settings.openai_api_key)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as audio_file:
            response = execute_openai_call_sync(
                call_type="audio_transcription",
                operation=lambda: client.audio.transcriptions.create(
                    model=settings.openai_transcribe_model,
                    file=audio_file,
                    language="pt",
                    prompt=(
                        "Transcrição de mensagem de WhatsApp em português do Brasil sobre "
                        "sorteios New Store, saldo, cartão presente, relógios e simulação de compra."
                    ),
                ),
            )
    except APIStatusError as exc:
        raise RuntimeError(f"openai_transcription_failed_{exc.status_code}") from exc
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise RuntimeError("empty_transcription")
    return text


def synthesize_reply_audio(text: str) -> tuple[bytes, str, str]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("openai_api_key_missing")

    trimmed = (text or "").strip()
    if not trimmed:
        raise RuntimeError("empty_tts_text")

    response_format = settings.openai_tts_format
    client = OpenAI(api_key=settings.openai_api_key)
    try:
        response = execute_openai_call_sync(
            call_type="audio_tts",
            operation=lambda: client.audio.speech.create(
                model=settings.openai_tts_model,
                voice=settings.openai_tts_voice,
                input=trimmed[:4096],
                response_format=response_format,
            ),
        )
    except APIStatusError as exc:
        raise RuntimeError(f"openai_tts_failed_{exc.status_code}") from exc

    if response_format == "opus":
        return response.content, "audio/ogg; codecs=opus", "resposta.ogg"
    return response.content, "audio/mpeg", "resposta.mp3"
