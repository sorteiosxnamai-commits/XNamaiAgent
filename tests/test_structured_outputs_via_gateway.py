"""Phase 2: business modules must not call chat.completions.parse directly."""

from pathlib import Path


def test_no_direct_chat_completions_parse_outside_gateway():
    root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name in {"openai_gateway.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "chat.completions.parse" in text:
            offenders.append(str(path.relative_to(root.parent)))
    assert offenders == [], (
        "Structured Outputs must go through parse_structured_output / gateway; "
        f"found direct parse in: {offenders}"
    )
