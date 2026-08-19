from app.vip_profiles import (
    FELIPE_NEWBOLD,
    build_vip_balance_reply,
    get_vip_profile,
    pick_vip_nickname,
)


def test_get_vip_profile_matches_felipe_phone():
    assert get_vip_profile("21969544700") == FELIPE_NEWBOLD
    assert get_vip_profile("+55 21 96954-4700") == FELIPE_NEWBOLD
    assert get_vip_profile("85999498149") is None


def test_pick_vip_nickname_from_known_set():
    nickname = pick_vip_nickname(FELIPE_NEWBOLD, "saldo")
    assert nickname in FELIPE_NEWBOLD.nicknames


def test_build_vip_balance_reply_is_personalized():
    reply = build_vip_balance_reply(FELIPE_NEWBOLD, "Big Boss", "R$ 50,00")
    assert "Big Boss" in reply
    assert "Felipe Newbold" in reply
    assert "R$ 50,00" in reply
