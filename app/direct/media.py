"""Prepare media for the same model; no legacy visual reasoning pipeline."""
import base64
from urllib.parse import urljoin


async def transcribe_audio(url):
    """Bounded download with validation at each redirect; async transcription."""
    import httpx
    from app.audio_service import _extension_for_content_type
    from app.config import get_settings
    from app.instagram_story_media import validate_story_media_url
    from app.openai_client import get_async_openai_client
    from app.openai_runtime import execute_openai_call
    current = url
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for _ in range(4):
            current, _ = validate_story_media_url(current)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    current = urljoin(current, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";")[0].lower()
                if not (mime.startswith("audio/") or mime == "application/ogg"):
                    raise ValueError("unsupported_audio")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 12_000_000:
                        raise ValueError("audio_too_large")
                if not data:
                    raise ValueError("empty_audio")
                cfg = get_settings()
                ai = get_async_openai_client().with_options(max_retries=0, timeout=20)
                transcription = await execute_openai_call(call_type="audio_transcription",
                    model=cfg.openai_transcribe_model,
                    operation=lambda: ai.audio.transcriptions.create(model=cfg.openai_transcribe_model,
                        file=("audio" + _extension_for_content_type(mime), bytes(data), mime), language="pt"))
                if not transcription.text.strip():
                    raise ValueError("empty_transcription")
                return transcription.text.strip()
    raise ValueError("audio_redirect_limit")


class _NoStorage:
    async def put_private(self, **kwargs):
        return None


async def input_content(incoming):
    text = incoming.text or "[Mídia recebida]"
    if incoming.audio_url and not incoming.transcription_failed:
        try:
            text = await transcribe_audio(incoming.audio_url)
            incoming.text = text
        except Exception:
            text += "\n[Áudio indisponível: peça texto ou reenvio.]"
    if incoming.transcription_failed:
        text += "\n[Transcrição indisponível: não suponha o conteúdo.]"
    content = [{"type": "input_text", "text": text}]
    image_url = incoming.image_url
    story = incoming.instagram_story
    if story and getattr(story, "media_type", None) == "image":
        image_url = story.operational_media_url() or image_url
    if story and getattr(story, "media_type", None) == "video":
        content[0]["text"] += "\n[Vídeo de Story indisponível neste caminho; peça uma foto.]"
    if image_url:
        from app.instagram_story_media import download_story_media
        try:
            media = await download_story_media(image_url, storage=_NoStorage())
            if media.content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise ValueError("unsupported_image")
            content.append({"type": "input_image", "detail": "auto", "image_url":
                f"data:{media.content_type};base64,{base64.b64encode(media.content).decode()}"})
        except Exception:
            content[0]["text"] += "\n[Imagem indisponível: peça reenvio ou descrição; não identifique por suposição.]"
    return content
