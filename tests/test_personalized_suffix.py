from app.agent_replies import _personalized_suffix


def test_personalized_suffix_skips_when_display_name_exists():
    suffix = _personalized_suffix(
        user_id=1,
        preferences={"preferred_name": None, "ask_preferred_name": True},
        phone="85999498149",
        display_name="Tironi",
    )
    assert suffix == ""


def test_personalized_suffix_skips_when_name_prompt_disabled():
    suffix = _personalized_suffix(
        user_id=1,
        preferences={"preferred_name": None, "ask_preferred_name": False},
        phone="85999498149",
        display_name=None,
    )
    assert suffix == ""
