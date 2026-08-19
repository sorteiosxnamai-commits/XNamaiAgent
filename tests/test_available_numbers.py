from app.audio_service import is_placeholder_audio_text, should_transcribe_incoming
from app.repository import (
    collect_taken_numbers,
    compute_available_numbers,
    default_draw_number_pool,
    normalize_draw_number,
    parse_draw_number_pool,
    resolve_available_numbers,
)


def test_default_pool_is_00_to_99():
    pool = default_draw_number_pool()
    assert len(pool) == 100
    assert pool[0] == "00"
    assert pool[99] == "99"


def test_parse_draw_number_pool_defaults_for_open_draw():
    pool = parse_draw_number_pool({"id": 116, "status": "open"})
    assert len(pool) == 100
    assert pool[3] == "03"


def test_normalize_draw_number():
    assert normalize_draw_number("3") == "03"
    assert normalize_draw_number("03") == "03"
    assert normalize_draw_number("97") == "97"


def test_available_numbers_exclude_only_approved():
    draw = {"id": 116}
    rows = [
        {"numbers": ["00"], "status": "approved"},
        {"numbers": ["01"], "status": "approved"},
        {"numbers": ["02"], "status": "approved"},
        {"numbers": ["03"], "status": "pending"},
    ]
    available = resolve_available_numbers(draw, rows)
    assert "00" not in available
    assert "01" not in available
    assert "02" not in available
    assert "03" in available
    assert "12" in available
    assert len(available) == 97


def test_should_transcribe_when_text_is_filename():
    assert should_transcribe_incoming("audio.ogg", "https://x/audio.ogg", "audio.ogg") is True


def test_is_placeholder_audio_text():
    assert is_placeholder_audio_text("audio.ogg", "audio.ogg") is True
    assert is_placeholder_audio_text("qual meu saldo", None) is False
