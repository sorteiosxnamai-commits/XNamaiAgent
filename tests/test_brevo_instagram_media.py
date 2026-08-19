from app.brevo_instagram_media import (
    PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY,
    UNVIEWABLE_MEDIA_GUIDE_REPLY,
    is_bare_price_request,
    is_brevo_unviewable_media_text,
    should_guide_instagram_price_without_media,
)
from app.models import IncomingMessage


def test_detects_brevo_unviewable_english_placeholder():
    text = (
        "⚠️ *This message cannot be viewed in Brevo.* "
        "Please go to Instagram app to view it."
    )
    assert is_brevo_unviewable_media_text(text) is True


def test_rejects_normal_visitor_text():
    assert is_brevo_unviewable_media_text("valor") is False
    assert is_brevo_unviewable_media_text("quero esse relógio") is False


def test_bare_price_requests():
    assert is_bare_price_request("valor") is True
    assert is_bare_price_request("Valor") is True
    assert is_bare_price_request("qual o preço") is True
    assert is_bare_price_request("quanto custa o kingfisher") is False


def test_should_guide_instagram_price_without_media():
    bare = IncomingMessage(channel="instagram", text="valor")
    assert should_guide_instagram_price_without_media(bare) is True

    with_image = IncomingMessage(
        channel="instagram",
        text="valor",
        image_url="https://cdn.example/watch.jpg",
        attachment_type="image",
    )
    assert should_guide_instagram_price_without_media(with_image) is False

    whatsapp = IncomingMessage(channel="whatsapp", text="valor")
    assert should_guide_instagram_price_without_media(whatsapp) is False


def test_guide_replies_are_actionable():
    assert "reenviar" in UNVIEWABLE_MEDIA_GUIDE_REPLY.casefold()
    assert "foto" in PRICE_WITHOUT_IMAGE_INSTAGRAM_REPLY.casefold()
